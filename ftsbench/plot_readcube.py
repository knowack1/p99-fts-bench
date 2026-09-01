"""Render the read-path cube: concurrency sweeps and SLA heatmaps (S18-S26).

Two modes over the same cell artifacts (`ftsbench.cell_bench`):

`--mode sweep` — one (query class, top-N): dual panel, p99 vs concurrency on
top, achieved throughput vs concurrency below, every repetition a thin line,
bold per-point median, the SLA drawn across both panels. The annotated number
is each config's max achieved throughput at the last rung whose median p99 is
under the SLA — the "read straight down from the SLA crossing" number.

    python3 -m ftsbench.plot_readcube --mode sweep \\
        --query-class rare_term --limit 10 \\
        --config opensearch:'data/readcube/cell-opensearch-refresh3-*.jsonl' \\
        --config scylla-cdc:'data/readcube/cell-scylla-cdc-*.jsonl' \\
        --output results/read-rare-l10.png

`--mode heatmap` — a grid of median p99 colored against the SLA, one panel
per config, shared scale. The projection is chosen by --rows:

    --rows limit --query-class rare_term      # S21: top-N x concurrency
    --rows class --limit 10                   # S25: class x concurrency
    --rows class --cols limit --concurrency 64  # S26: class x top-N at c=64

Cells that failed their gates never reach this module (the driver renames
them .failed); a (row, col) with no surviving cells renders as a hatched gap,
never as a zero — an empty cell is a hole in the record, not a fast engine.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from ftsbench import plotlib

DEFAULT_OUTPUT = "results/readcube.png"
DEFAULT_SLA_MS = 10.0
CLASS_ORDER = ("rare_term", "common_term", "phrase",
               "bool_and", "bool_not", "bool_mixed")


def cell_key(run: plotlib.Run) -> tuple[str, int, int, int]:
    summary = run.records[-1]
    return (str(summary["query_class"]), int(summary["limit"]),
            int(summary["concurrency"]), int(summary["rep"]))


def cell_p99(run: plotlib.Run) -> float:
    value = run.records[-1].get("p99_ms")
    return float(value) if value is not None else math.nan


def cell_qps(run: plotlib.Run) -> float:
    return float(run.records[-1].get("achieved_qps") or math.nan)


def usable(run: plotlib.Run) -> str:
    if not run.records:
        return "no summary record"
    summary = run.records[-1]
    if summary.get("completed", 0) <= 0:
        return "no completed operations"
    if summary.get("errors", 0) > 0:
        return f"{summary['errors']} errors"
    return ""


def select(config: plotlib.ConfigSeries, **want: Any) -> list[plotlib.Run]:
    keep = []
    for run in config.runs:
        summary = run.records[-1]
        if all(str(summary.get(field)) == str(value)
               for field, value in want.items()):
            keep.append(run)
    return keep


def by_rep_lines(runs: Sequence[plotlib.Run],
                 value: Any) -> dict[int, list[tuple[int, float]]]:
    lines: dict[int, list[tuple[int, float]]] = {}
    for run in runs:
        _, _, conc, rep = cell_key(run)
        lines.setdefault(rep, []).append((conc, value(run)))
    return {rep: sorted(points) for rep, points in lines.items()}


def median_line(lines: dict[int, list[tuple[int, float]]]
                ) -> list[tuple[int, float]]:
    by_conc: dict[int, list[float]] = {}
    for points in lines.values():
        for conc, val in points:
            if not math.isnan(val):
                by_conc.setdefault(conc, []).append(val)
    return [(conc, statistics.median(vals))
            for conc, vals in sorted(by_conc.items()) if len(vals) >= 2]


def draw_panel(axes: Any, config: plotlib.ConfigSeries, index: int,
               runs: Sequence[plotlib.Run], value: Any, label: str) -> None:
    style = plotlib.style_for(config.name, index)
    lines = by_rep_lines(runs, value)
    for points in lines.values():
        xs = [c for c, _ in points]
        ys = [v for _, v in points]
        axes.plot(xs, ys, color=style["color"], linewidth=1.0, alpha=0.3)
    med = median_line(lines)
    if med:
        axes.plot([c for c, _ in med], [v for _, v in med],
                  color=style["color"], linestyle=style["linestyle"],
                  linewidth=2.6, label=f"{config.name} {label}")


def max_qps_under_sla(runs: Sequence[plotlib.Run], sla_ms: float) -> tuple[int, float] | None:
    p99 = dict(median_line(by_rep_lines(runs, cell_p99)))
    qps = dict(median_line(by_rep_lines(runs, cell_qps)))
    under = [conc for conc, val in sorted(p99.items()) if val <= sla_ms]
    if not under:
        return None
    best = under[-1]
    return best, qps.get(best, math.nan)


def plot_sweep(args: Any, configs: list[plotlib.ConfigSeries]
               ) -> tuple[Any, dict[str, Any]]:
    figure, (top, bottom) = plotlib.plt.subplots(
        2, 1, sharex=True, figsize=(args.width, args.height))
    notes = [f"closed-loop: N in flight, next query on return; SLA "
             f"{args.sla_ms:g} ms; achieved = completed / wall"]
    per_config: dict[str, dict[str, Any]] = {}
    for index, config in enumerate(configs):
        runs = select(config, query_class=args.query_class, limit=args.limit)
        if not runs:
            notes.append(f"{config.name}: no cells for "
                         f"{args.query_class} l={args.limit}")
            continue
        draw_panel(top, config, index, runs, cell_p99, "p99")
        draw_panel(bottom, config, index, runs, cell_qps, "achieved")
        crossing = max_qps_under_sla(runs, args.sla_ms)
        per_config[config.name] = {
            "cells": len(runs),
            "max_under_sla": ({"concurrency": crossing[0],
                               "achieved_qps": crossing[1]}
                              if crossing else None)}
        if crossing:
            bottom.annotate(f"{config.name}: {crossing[1]:,.0f} qps under SLA",
                            xy=(crossing[0], crossing[1]), fontsize=9,
                            xytext=(5, 5), textcoords="offset points")
    for axes in (top, bottom):
        axes.set_xscale("log", base=2)
        axes.grid(True, alpha=0.25)
    top.axhline(args.sla_ms, color="#8c1d13", linewidth=1.0, linestyle=":")
    top.set_ylabel("p99, ms")
    top.set_yscale("log")
    bottom.set_ylabel("achieved queries/s")
    bottom.set_xlabel("concurrency (workers in flight)")
    plotlib.title_and_subtitle(top, args)
    top.legend(fontsize=9)
    return figure, {"metric_name": "cell p99_ms", "notes": notes,
                    "per_config": per_config, "layout_top": 0.93}


def heatmap_axes_values(args: Any) -> tuple[str, list, str, list]:
    if args.rows == "limit":
        return "top-N", [10, 100, 1000], "concurrency", []
    if args.cols == "limit":
        return "query class", list(CLASS_ORDER), "top-N", [10, 100, 1000]
    return "query class", list(CLASS_ORDER), "concurrency", []


def heatmap_cell_filter(args: Any, row: Any, col: Any) -> dict[str, Any]:
    want: dict[str, Any] = {}
    if args.rows == "limit":
        want.update(query_class=args.query_class, limit=row)
    else:
        want.update(query_class=row)
        want.update({"limit": col} if args.cols == "limit"
                    else {"limit": args.limit})
    if args.cols != "limit":
        want["concurrency"] = col
    else:
        want["concurrency"] = args.concurrency
    return want


def concurrency_values(configs: list[plotlib.ConfigSeries]) -> list[int]:
    values = {cell_key(run)[2] for config in configs for run in config.runs}
    return sorted(values)


def plot_heatmap(args: Any, configs: list[plotlib.ConfigSeries]
                 ) -> tuple[Any, dict[str, Any]]:
    row_label, rows, col_label, cols = heatmap_axes_values(args)
    if not cols:
        cols = concurrency_values(configs)
    figure, panels = plotlib.plt.subplots(
        1, len(configs), figsize=(args.width, args.height), squeeze=False)
    vmax = max(args.sla_ms * 4, 1.0)
    mesh = None
    for index, config in enumerate(configs):
        axes = panels[0][index]
        grid = []
        for row in rows:
            line = []
            for col in cols:
                runs = select(config, **heatmap_cell_filter(args, row, col))
                vals = [cell_p99(r) for r in runs
                        if not math.isnan(cell_p99(r))]
                line.append(statistics.median(vals) if len(vals) >= 2
                            else math.nan)
            grid.append(line)
        mesh = axes.pcolormesh(
            range(len(cols) + 1), range(len(rows) + 1), grid,
            cmap="RdYlGn_r", vmin=0, vmax=vmax, edgecolors="white",
            linewidth=0.5)
        axes.set_xticks([i + 0.5 for i in range(len(cols))], cols, fontsize=8)
        axes.set_yticks([i + 0.5 for i in range(len(rows))], rows, fontsize=8)
        axes.set_xlabel(col_label)
        if index == 0:
            axes.set_ylabel(row_label)
        axes.set_title(config.name, fontsize=11)
        for r, row_vals in enumerate(grid):
            for c, val in enumerate(row_vals):
                if math.isnan(val):
                    axes.text(c + 0.5, r + 0.5, "—", ha="center", va="center")
                else:
                    axes.text(c + 0.5, r + 0.5, f"{val:.0f}", ha="center",
                              va="center", fontsize=8)
    if mesh is not None:
        bar = figure.colorbar(mesh, ax=[p for row in panels for p in row],
                              fraction=0.03)
        bar.set_label(f"median p99, ms (SLA {args.sla_ms:g})", fontsize=8)
        bar.ax.axhline(args.sla_ms, color="black", linewidth=1.2)
    plotlib.figure_title(figure, args)
    return figure, {"metric_name": "cell p99_ms",
                    "notes": [f"cell = median p99 across repetitions; SLA "
                              f"{args.sla_ms:g} ms; — = no surviving cells"],
                    "layout_top": 0.88}


def main() -> int:
    import sys
    mode = "sweep" if "--mode" not in " ".join(sys.argv) or "sweep" in sys.argv else "heatmap"
    chart = "READ-SWEEP" if mode == "sweep" else "READ-HEATMAP"
    parser = plotlib.build_parser(chart, __doc__, DEFAULT_OUTPUT,
                                  "Read path", "")
    parser.add_argument("--mode", choices=["sweep", "heatmap"], required=True)
    parser.add_argument("--query-class", default="rare_term")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--rows", choices=["class", "limit"], default="class")
    parser.add_argument("--cols", choices=["concurrency", "limit"],
                        default="concurrency")
    parser.add_argument("--sla-ms", type=float, default=DEFAULT_SLA_MS)
    args = parser.parse_args()
    plot = plot_sweep if args.mode == "sweep" else plot_heatmap
    return plotlib.emit(args, "cell_summary", cell_p99, plot, usable)


if __name__ == "__main__":
    raise SystemExit(main())
