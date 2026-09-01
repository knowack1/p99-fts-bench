"""Render the write-path growth charts: docs/s, CPU or RSS vs indexed documents.

One module, three charts (deck S13/S14/S15), selected by --metric:

    python3 -m ftsbench.plot_growth --metric rate \\
        --config opensearch:'data/c1-opensearch-[0-9]*.jsonl' \\
        --config scylla-cdc:'data/c1-scylla-cdc-*.jsonl' \\
        --output results/growth-rate.png

    python3 -m ftsbench.plot_growth --metric cpu \\
        --config opensearch:'data/c1-opensearch-[0-9]*.jsonl' \\
        --probe  opensearch:'data/c4-opensearch-[0-9]*.jsonl' \\
        --config scylla-cdc:'data/c1-scylla-cdc-*.jsonl' \\
        --probe  scylla-cdc:'data/c4-scylla-cdc-*.jsonl' \\
        --output results/growth-cpu.png

Unlike C1 (one representative run vs wall time), every repetition is drawn as
a thin line and the bold line is the per-grid-point MEDIAN across repetitions
— the deck's "5 runs per engine (thin lines); bold = median" convention. A
pointwise median of five is robust to one cold or slow repetition without
hiding it: the outlier stays visible as its own thin line.

Three decisions this chart depends on:

- **The x axis is documents indexed, resampled onto one shared grid for every
  config.** The vector-store reports its count in ~10,000-document quanta, so
  a per-sample ScyllaDB line is partly a sampling artifact; resampling ONLY
  that side would smooth one engine and not the other. Both engines go
  through the same grid, and the grid step is stated in the footer.
- **Rate is computed from the grid, not read from samples.** rate at bucket k
  is (docs_k - docs_{k-1}) / (t(docs_k) - t(docs_{k-1})), where t(d) inverts
  the monotone docs-vs-time series by linear interpolation. This is the same
  quantity for both engines regardless of their sampling cadence.
- **CPU and RSS are joined to the build by absolute time.** The probe and the
  monitor start at different instants; each series' header `started_at`
  anchors t_elapsed_s to wall-clock time, and the probe value at t(docs_k) is
  what gets plotted. Per tick, CPU is the SUM of cpu_cores_used across the
  side's containers and RSS the sum of rss_bytes — the ScyllaDB side is two
  services and a single-container read would understate it by exactly the
  index's share (see plot_c4).
"""
from __future__ import annotations

import math
import os
import re
import statistics
import sys
from datetime import datetime
from typing import Any, Sequence

from ftsbench import plotlib

DEFAULT_OUTPUT = "results/growth.png"

METRICS = {
    "rate": {"chart": "GROWTH-RATE", "ylabel": "documents indexed per second",
             "title": "Build rate as the index grows"},
    "cpu": {"chart": "GROWTH-CPU", "ylabel": "CPU cores busy (side total)",
            "title": "CPU as the index grows"},
    "rss": {"chart": "GROWTH-RSS", "ylabel": "RSS, GiB (side total)",
            "title": "RAM (RSS) as the index grows"},
}

REP_RE = re.compile(r"-(\d+)\.jsonl$")
GRID_QUANTUM = 10_000
GRID_TARGET_BUCKETS = 80
THIN = {"linewidth": 1.0, "alpha": 0.35}
BOLD = {"linewidth": 2.6, "alpha": 1.0}


def rep_of(path: str) -> str:
    match = REP_RE.search(os.path.basename(path))
    return match.group(1) if match else ""


def started_at(run: plotlib.Run) -> float:
    return datetime.fromisoformat(str(run.header["started_at"])).timestamp()


def docs_timeline(run: plotlib.Run) -> list[tuple[float, float]]:
    """Monotone (absolute time, docs_indexed) pairs for one repetition."""
    base = started_at(run)
    points: list[tuple[float, float]] = []
    top = 0.0
    for record in run.records:
        docs = record.get("docs_indexed")
        if docs is None:
            continue
        top = max(top, float(docs))
        points.append((base + float(record["t_elapsed_s"]), top))
    return points


def interpolate(xs: Sequence[float], ys: Sequence[float], x: float) -> float | None:
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    for i in range(1, len(xs)):
        if x <= xs[i]:
            if xs[i] == xs[i - 1]:
                return ys[i]
            frac = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + frac * (ys[i] - ys[i - 1])
    return ys[-1]


def time_at_docs(timeline: Sequence[tuple[float, float]], docs: float) -> float | None:
    """First instant the count reaches `docs` — the inverse of the (monotone)
    docs-vs-time series, linearly interpolated inside each quantum step."""
    times = [t for t, _ in timeline]
    counts = [d for _, d in timeline]
    if not counts or docs > counts[-1]:
        return None
    return interpolate(counts, times, docs)


def docs_grid(step: int, totals: Sequence[float]) -> tuple[list[float], int]:
    total = min(totals)
    if step <= 0:
        step = max(GRID_QUANTUM,
                   math.ceil(total / GRID_TARGET_BUCKETS / GRID_QUANTUM)
                   * GRID_QUANTUM)
    return [float(d) for d in range(step, int(total) + 1, step)], step


def rate_line(timeline: Sequence[tuple[float, float]],
              grid: Sequence[float]) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    previous_t = time_at_docs(timeline, 0) or timeline[0][0]
    previous_d = 0.0
    for docs in grid:
        t = time_at_docs(timeline, docs)
        if t is None:
            break
        if t > previous_t:
            xs.append((previous_d + docs) / 2)
            ys.append((docs - previous_d) / (t - previous_t))
        previous_t, previous_d = t, docs
    return xs, ys


def side_totals(probe: plotlib.Run, field: str) -> list[tuple[float, float]]:
    """Per-tick (absolute time, sum of `field` across the side's containers)."""
    base = started_at(probe)
    by_tick: dict[int, dict[str, Any]] = {}
    for record in probe.records:
        tick = by_tick.setdefault(int(record["i"]), {"t": record["t_elapsed_s"],
                                                     "values": []})
        value = record.get(field)
        if value is not None:
            tick["values"].append(float(value))
    return [(base + tick["t"], sum(tick["values"]))
            for _, tick in sorted(by_tick.items()) if tick["values"]]


def probe_line(timeline: Sequence[tuple[float, float]],
               totals: Sequence[tuple[float, float]], grid: Sequence[float],
               scale: float) -> tuple[list[float], list[float]]:
    times = [t for t, _ in totals]
    values = [v for _, v in totals]
    xs, ys = [], []
    for docs in grid:
        t = time_at_docs(timeline, docs)
        value = interpolate(times, values, t) if t is not None else None
        if value is not None:
            xs.append(docs)
            ys.append(value * scale)
    return xs, ys


def pointwise_median(lines: Sequence[tuple[list[float], list[float]]]
                     ) -> tuple[list[float], list[float]]:
    by_x: dict[float, list[float]] = {}
    for xs, ys in lines:
        for x, y in zip(xs, ys):
            by_x.setdefault(x, []).append(y)
    xs_out, ys_out = [], []
    for x in sorted(by_x):
        if len(by_x[x]) >= 2:
            xs_out.append(x)
            ys_out.append(statistics.median(by_x[x]))
    return xs_out, ys_out


def probe_index(specs: list[str]) -> dict[tuple[str, str], plotlib.Run]:
    index: dict[tuple[str, str], plotlib.Run] = {}
    for spec in specs:
        name, pattern = plotlib.parse_config_spec(spec)
        for run in plotlib.load_runs(name, pattern, "resource_sample"):
            index[(name, rep_of(run.path))] = run
    return index


def overall_rate(run: plotlib.Run) -> float:
    timeline = docs_timeline(run)
    if len(timeline) < 2 or timeline[-1][1] <= 0:
        return 0.0
    wall = timeline[-1][0] - timeline[0][0]
    return timeline[-1][1] / wall if wall > 0 else 0.0


def usable(run: plotlib.Run) -> str:
    timeline = docs_timeline(run)
    if len(timeline) < 2:
        return "fewer than two samples"
    if timeline[-1][1] <= 0:
        return "no documents indexed"
    return ""


def config_lines(args: Any, config: plotlib.ConfigSeries,
                 probes: dict[tuple[str, str], plotlib.Run],
                 grid: Sequence[float]
                 ) -> tuple[list[tuple[list[float], list[float]]], list[str]]:
    lines, missing = [], []
    scale = 1.0 / 2**30 if args.metric == "rss" else 1.0
    field = "rss_bytes" if args.metric == "rss" else "cpu_cores_used"
    for run in config.runs:
        timeline = docs_timeline(run)
        if args.metric == "rate":
            lines.append(rate_line(timeline, grid))
            continue
        probe = probes.get((config.name, rep_of(run.path)))
        if probe is None:
            missing.append(f"{config.name} rep {rep_of(run.path)}: no probe file")
            continue
        lines.append(probe_line(timeline, side_totals(probe, field), grid, scale))
    return lines, missing


def draw(axes: Any, config: plotlib.ConfigSeries, index: int,
         lines: Sequence[tuple[list[float], list[float]]]) -> None:
    style = plotlib.style_for(config.name, index)
    for xs, ys in lines:
        if xs:
            axes.plot(xs, ys, color=style["color"], **THIN)
    xs, ys = pointwise_median(lines)
    if xs:
        axes.plot(xs, ys, color=style["color"], linestyle=style["linestyle"],
                  label=f"{config.name} (median of {len(lines)})", **BOLD)


def plot(args: Any, configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    probes = probe_index(args.probe or [])
    totals = [max(docs_timeline(run)[-1][1] for run in config.runs)
              for config in configs]
    grid, step = docs_grid(args.docs_step, totals)

    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    notes = [f"both engines resampled onto one {step:,}-document grid "
             "(the vector-store reports its count in ~10k quanta; "
             "resampling one side only would smooth one engine and not the other)"]
    per_config: dict[str, dict[str, Any]] = {}
    for index, config in enumerate(configs):
        lines, missing = config_lines(args, config, probes, grid)
        notes.extend(missing)
        draw(axes, config, index, lines)
        per_config[config.name] = {
            "lines_drawn": sum(1 for xs, _ in lines if xs),
            "docs_grid_step": step,
        }

    plotlib.frame(axes, args, "documents indexed", METRICS[args.metric]["ylabel"])
    axes.set_ylim(bottom=0)
    plotlib.title_and_subtitle(axes, args)
    axes.legend(loc="best", fontsize=9)
    return figure, {"metric_name": "docs_per_s_overall (run selection only; "
                                   "the chart draws every repetition)",
                    "notes": notes, "per_config": per_config,
                    "docs_grid_step": step}


def main() -> int:
    probe_peek = argparse_metric()
    parser = plotlib.build_parser(
        METRICS[probe_peek]["chart"], __doc__, DEFAULT_OUTPUT,
        METRICS[probe_peek]["title"], "", disclose_write_path=True)
    parser.add_argument("--metric", choices=sorted(METRICS), required=True)
    parser.add_argument("--probe", action="append", metavar="NAME:GLOB",
                        help="probe series per config (required for cpu/rss)")
    parser.add_argument("--docs-step", type=int, default=0,
                        help="x-grid step in documents (0 = auto, "
                             f"multiples of {GRID_QUANTUM})")
    args = parser.parse_args()
    if args.metric != "rate" and not args.probe:
        parser.error(f"--metric {args.metric} needs --probe NAME:GLOB per config")
    return plotlib.emit(args, "sample", overall_rate, plot, usable)


def argparse_metric() -> str:
    """The chart id and default title depend on --metric, which argparse only
    yields after the parser exists — so peek at argv first."""
    for i, argument in enumerate(sys.argv):
        if argument == "--metric" and i + 1 < len(sys.argv):
            return sys.argv[i + 1] if sys.argv[i + 1] in METRICS else "rate"
        if argument.startswith("--metric="):
            value = argument.split("=", 1)[1]
            return value if value in METRICS else "rate"
    return "rate"


if __name__ == "__main__":
    raise SystemExit(main())
