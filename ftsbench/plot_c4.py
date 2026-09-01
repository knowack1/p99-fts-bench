"""Render chart C4 — resource footprint: RAM, CPU and index size on disk.

Three panels of grouped bars, one bar per *role*. The ScyllaDB side is drawn
**split and summed**: `scylladb`, `vector-store` and their total. Per
../CLAUDE.md the architecture is a ScyllaDB cluster plus a separate
vector-store cluster holding an in-RAM index, so a single merged bar would
misrepresent it — and the in-RAM Tantivy index is exactly why that RAM bar is
bigger, which COMPARABILITY.md commits to showing openly rather than defending.

Two conventions this chart depends on:

- **The total RAM bar is the peak of the per-tick sum**, not the sum of the
  per-role peaks: the latter adds two maxima that may never have coexisted.
- **A null `index_size_bytes` is never a zero bar.** The in-RAM index has no
  on-disk size; the bar is omitted and annotated "in RAM — see RAM bar", so the
  cost is attributed to the panel that actually carries it.

    python3 -m ftsbench.plot_c4 \\
        --config opensearch:'data/c4-opensearch-[0-9]*.jsonl' \\
        --config scylla-cdc:data/c4-scylla-cdc-*.jsonl --output results/c4.png

The opensearch glob excludes refresh30 deliberately: data/c4-opensearch-*.jsonl
also matches every c4-opensearch-refresh30-* file, which would count each
refresh=30s repetition under both configurations.
"""
import argparse
import collections
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Callable

from . import plotlib

CHART = "C4"
RECORD = "resource_sample"
DEFAULT_OUTPUT = "results/c4.png"
DEFAULT_TITLE = "Resource footprint"
DEFAULT_SUBTITLE = ("ScyllaDB is shown split by role and summed; the in-RAM "
                    "Tantivy index has no on-disk size and is annotated, not zeroed")
TOTAL = "total"
GB = 1e9
IN_RAM_NOTE = "in RAM —\nsee RAM bar"
METRIC_NAME = "peak_total_rss_bytes"


@dataclass(frozen=True)
class Bar:
    """One role of one config, as the three panels need it."""

    config: str
    role: str
    ram_bytes: float
    cpu_cores: float
    index_bytes: float | None
    index_in_ram: bool
    ticks_not_running: int = 0

    @property
    def label(self) -> str:
        return f"{self.config}\n{self.role}"


@dataclass(frozen=True)
class Panel:
    name: str
    title: str
    ylabel: str
    value: Callable[[Bar], float | None]
    fmt: str


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(
        CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE, DEFAULT_SUBTITLE,
        disclose_write_path=True, width=12.0, height=6.5)
    return parser.parse_args()


def group_by(records: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        grouped[record.get(key)].append(record)
    return dict(grouped)


def reported_rss(records: list[dict[str, Any]]) -> list[float]:
    """Only the samples that actually reported RSS.

    A tick whose cgroup read failed reports none. Coercing that to 0.0 puts a
    fabricated empty container into the peak-of-the-sum, and if the unmeasured
    tick is the one holding the true peak the reported peak silently moves to a
    lower tick — same reasoning as reported_cores.
    """
    return [float(item["rss_bytes"]) for item in records
            if item.get("rss_bytes") is not None]


def peak_rss(records: list[dict[str, Any]]) -> float:
    return max(reported_rss(records), default=0.0)


def reported_cores(records: list[dict[str, Any]]) -> list[float]:
    """Only the samples that actually reported CPU.

    The first tick reports none by construction: cores-used is a delta between
    two reads of a cumulative counter, and the first read has nothing to subtract
    from. Coercing that null to 0.0 would feed a fabricated idle sample into
    every mean, so a short run's CPU bar would sit visibly below the truth.
    """
    return [float(item["cpu_cores_used"]) for item in records
            if item.get("cpu_cores_used") is not None]


def mean_cores(records: list[dict[str, Any]]) -> float:
    values = reported_cores(records)
    return statistics.fmean(values) if values else 0.0


def index_bytes(records: list[dict[str, Any]]) -> float | None:
    """The last reported on-disk size. None means the engine reports none —
    the in-RAM Tantivy index — and must not become a zero."""
    reported = [float(item["index_size_bytes"]) for item in records
                if item.get("index_size_bytes") is not None]
    return reported[-1] if reported else None


def rss_tick_sums(records: list[dict[str, Any]]) -> list[float]:
    """Per-tick RSS totals, skipping any tick that reported no RSS at all rather
    than summing its nulls to a zero — see reported_rss."""
    ticks = group_by(records, "i")
    reported = [reported_rss(tick) for tick in ticks.values()]
    return [sum(values) for values in reported if values]


def peak_total_rss(records: list[dict[str, Any]]) -> float:
    return max(rss_tick_sums(records), default=0.0)


def run_peak_total_rss(run: plotlib.Run) -> float:
    return peak_total_rss(run.records)


def cpu_tick_sums(records: list[dict[str, Any]]) -> list[float]:
    """Per-tick CPU totals, skipping any tick that reported no CPU at all
    rather than summing its nulls to a zero — see reported_cores."""
    ticks = group_by(records, "i")
    reported = [reported_cores(tick) for tick in ticks.values()]
    return [sum(values) for values in reported if values]


def mean_total_cores(records: list[dict[str, Any]]) -> float:
    sums = cpu_tick_sums(records)
    return statistics.fmean(sums) if sums else 0.0


def ticks_not_running(records: list[dict[str, Any]]) -> int:
    """Samples taken while the container was not running.

    A container that exited mid-run keeps producing samples, and averaging over
    them reports a footprint for a process that was gone. That is the difference
    between a cheap engine and a dead one, so it is counted and annotated rather
    than averaged in silently.
    """
    return sum(1 for item in records if item.get("running") is False)


def total_index_bytes(roles: dict[str, list[dict[str, Any]]]) -> float | None:
    parts = [index_bytes(records) for records in roles.values()]
    reported = [part for part in parts if part is not None]
    return sum(reported) if reported else None


def role_bar(config: str, role: str, records: list[dict[str, Any]]) -> Bar:
    size = index_bytes(records)
    return Bar(config=config, role=role, ram_bytes=peak_rss(records),
               cpu_cores=mean_cores(records), index_bytes=size,
               index_in_ram=size is None,
               ticks_not_running=ticks_not_running(records))


def total_bar(config: str, records: list[dict[str, Any]],
              roles: dict[str, list[dict[str, Any]]]) -> Bar:
    size = total_index_bytes(roles)
    partial = any(index_bytes(part) is None for part in roles.values())
    return Bar(config=config, role=TOTAL, ram_bytes=peak_total_rss(records),
               cpu_cores=mean_total_cores(records), index_bytes=size,
               index_in_ram=partial,
               ticks_not_running=ticks_not_running(records))


def bars_for(config: plotlib.ConfigSeries) -> list[Bar]:
    records = config.chosen.records
    roles = group_by(records, "role")
    bars = [role_bar(config.name, str(role), part)
            for role, part in sorted(roles.items(), key=lambda item: str(item[0]))]
    if len(roles) > 1:
        bars.append(total_bar(config.name, records, roles))
    return bars


def panels() -> tuple[Panel, ...]:
    return (
        Panel("ram", "RAM (peak anon RSS)", "GB",
              lambda bar: bar.ram_bytes / GB, "{:,.2f}"),
        Panel("cpu", "CPU (mean cores in use)", "cores",
              lambda bar: bar.cpu_cores, "{:,.2f}"),
        Panel("index", "index size on disk", "GB",
              lambda bar: None if bar.index_bytes is None else bar.index_bytes / GB,
              "{:,.2f}"),
    )


def bar_color(bar: Bar) -> str:
    return plotlib.ROLE_COLORS.get(bar.role, plotlib.ROLE_COLORS[TOTAL])


def draw_value(axes: Any, position: int, value: float, text: str) -> None:
    axes.annotate(text, (position, value), textcoords="offset points",
                  xytext=(0, 4), ha="center", fontsize=8)


def draw_missing(axes: Any, position: int, bar: Bar) -> None:
    note = IN_RAM_NOTE if bar.role != TOTAL else "partly in RAM —\nsee RAM bar"
    axes.annotate(note, (position, 0.0), textcoords="offset points", xytext=(0, 12),
                  ha="center", fontsize=7.5, color="#8c1d13", weight="bold")


def draw_partial_total(axes: Any, position: int, value: float) -> None:
    axes.annotate("excludes the\nin-RAM index", (position, value),
                  textcoords="offset points", xytext=(20, -26), ha="right",
                  fontsize=7.5, color="#8c1d13")


def draw_bar(axes: Any, panel: Panel, position: int, bar: Bar, value: float) -> None:
    axes.bar(position, value, width=0.6, color=bar_color(bar),
             alpha=0.55 if bar.role == TOTAL else 0.9, zorder=2,
             hatch="//" if bar.role == TOTAL else None)
    draw_value(axes, position, value, panel.fmt.format(value))
    if panel.name == "index" and bar.index_in_ram:
        draw_partial_total(axes, position, value)


def draw_panel(axes: Any, panel: Panel, bars: list[Bar]) -> None:
    for position, bar in enumerate(bars):
        value = panel.value(bar)
        if value is None:
            draw_missing(axes, position, bar)
            continue
        draw_bar(axes, panel, position, bar, value)
    label_panel(axes, panel, bars)


def label_panel(axes: Any, panel: Panel, bars: list[Bar]) -> None:
    axes.set_xticks(range(len(bars)))
    axes.set_xticklabels([bar.label for bar in bars], fontsize=8)
    axes.set_title(panel.title, fontsize=10, loc="left")
    axes.set_ylabel(panel.ylabel)
    axes.set_ylim(bottom=0)
    axes.grid(True, axis="y", alpha=0.25, linewidth=0.6)
    axes.set_axisbelow(True)


def in_ram_note(bars: list[Bar]) -> str:
    affected = [f"{bar.config}/{bar.role}" for bar in bars
                if bar.index_in_ram and bar.role != TOTAL]
    if not affected:
        return ""
    return ("index_size_bytes is null for " + ", ".join(affected) +
            " — the index is in RAM and is rebuilt by a full table scan on "
            "restart; its cost is in the RAM panel, so no bar is drawn here")


def total_note(bars: list[Bar]) -> str:
    if not any(bar.role == TOTAL for bar in bars):
        return ""
    return ("hatched 'total' bars are the peak of the per-tick sum (RAM) and the "
            "mean of the per-tick sum (CPU), not the sum of per-role peaks")


def not_running_note(bars: list[Bar]) -> str:
    affected = [f"{bar.config}/{bar.role} ({bar.ticks_not_running} samples)"
                for bar in bars
                if bar.ticks_not_running and bar.role != TOTAL]
    if not affected:
        return ""
    return ("the container was NOT RUNNING for part of the sampled window in " +
            ", ".join(affected) + " — these bars average over a window in which "
            "the process was absent and understate the footprint; treat the "
            "affected config as a failed repetition, not a cheap one")


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    bars = [bar for config in configs for bar in bars_for(config)]
    figure, axes_row = plotlib.plt.subplots(1, 3, figsize=(args.width, args.height))
    for axes, panel in zip(axes_row, panels()):
        draw_panel(axes, panel, bars)
    plotlib.figure_title(figure, args)
    return figure, extras(configs, bars)


def bar_sidecar(bar: Bar) -> dict[str, Any]:
    return {
        "role": bar.role,
        "peak_rss_bytes": bar.ram_bytes,
        "mean_cpu_cores_used": bar.cpu_cores,
        "index_size_bytes": bar.index_bytes,
        "index_in_ram": bar.index_in_ram,
        "ticks_not_running": bar.ticks_not_running,
    }


def extras(configs: list[plotlib.ConfigSeries],
           bars: list[Bar]) -> dict[str, Any]:
    notes = [in_ram_note(bars), total_note(bars), not_running_note(bars)]
    per_config = {
        config.name: {"roles": [bar_sidecar(bar) for bar in bars
                                if bar.config == config.name]}
        for config in configs
    }
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "layout_top": 0.87,
        "panels": [panel.title for panel in panels()],
        "ram_definition": "cgroup anon RSS, not memory.current — page cache would "
                          "flatter whichever engine touched less disk",
        "per_config": per_config,
    }


def unusable_reason(run: plotlib.Run) -> str:
    """A repetition whose container never ran is not a cheap one.

    Its RSS and CPU score near zero, and median selection over two repetitions
    takes the lower of the two — so without this the crashed run wins the median
    and the chart reports a footprint for a process that was absent. The footer
    note covers a partly-absent window; this excludes a wholly absent one.
    """
    roles = group_by(run.records, "role")
    absent = sorted(str(role) for role, records in roles.items()
                    if records and ticks_not_running(records) == len(records))
    if not absent:
        return ""
    return f"container never running in any sampled tick: {', '.join(absent)}"


def main() -> int:
    args = parse_args()
    return plotlib.emit(args, RECORD, run_peak_total_rss, plot,
                        usable=unusable_reason)


if __name__ == "__main__":
    sys.exit(main())
