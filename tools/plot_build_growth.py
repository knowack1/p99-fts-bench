#!/usr/bin/env python3
"""The harness's build rate against the index it has already built.

X is documents indexed by this level, Y is documents indexed per second. A
point CSV answers "how fast was this level, on average"; this answers "what did
the build do while it was happening" — the shape an average cannot carry: a
client racing ahead of its index, a stall, and the tail after the client stops
and the index is still draining.

Input is the per-second series `scyllarate --samples-dir` writes, one file per
level, named `c<concurrency>-<repetition>.csv`. Repetitions of one concurrency
are thin lines and their pointwise median is the bold one, the convention the
deck's growth charts (`ftsbench/plot_growth.py`) already use — and this borrows
that module's grid, because the two charts have to mean the same thing.

    .venv/bin/python3 tools/plot_build_growth.py \\
        --samples '<R>/samples/c*.csv' \\
        --output  '<R>/build-growth.png' \\
        --table   '<R>/build-growth.csv' \\
        --subtitle '<RUN_ID> · i8g.2xlarge · null sink'

A DIAGNOSTIC chart. The sink is a null sink: it answers the vector-store status
endpoint and discards every row, so the ceiling on it is the harness's, not an
engine's. **No number from it is an engine number.**
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Sequence

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftsbench.plot_growth import (docs_grid, interpolate,  # noqa: E402
                                  pointwise_median, rate_line)
from harness_charts import (MARKERS, colours, draw_footer,  # noqa: E402
                            label_right_edge, read_csv_rows, write_table)

LEVEL_RE = re.compile(r"c(\d+)-(\d+)\.csv$")
MIN_READINGS = 3
THIN = {"linewidth": 1.0, "alpha": 0.40}
BOLD = {"linewidth": 2.4, "alpha": 1.0}
TABLE_COLUMNS = ["series", "reps", "docs_indexed", "index_docs_per_s"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", required=True,
                        help="glob of scyllarate series CSVs, e.g. '<dir>/c*.csv'")
    parser.add_argument("--output", required=True, help="PNG path")
    parser.add_argument("--table", default="", help="also write every plotted point as CSV")
    parser.add_argument("--grid-step", type=int, default=0,
                        help="documents per bucket; 0 picks one from the corpus size")
    parser.add_argument("--title", default="Build rate as the index grows (harness, null sink)")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)
    return parser.parse_args()


class Level:
    """One level's series: its index build, and where its client stopped."""

    def __init__(self, path: Path, rows: Sequence[dict]) -> None:
        self.concurrency, self.repetition = name_parts(path)
        self.timeline = index_timeline(rows)
        self.handover_docs = handover_docs(rows)

    @property
    def readings(self) -> int:
        """Readings the level took, which is one fewer than the points on its
        timeline: the level's start is known, not read."""
        return len(self.timeline) - 1

    @property
    def series(self) -> str:
        return f"c={self.concurrency}"

    @property
    def indexed(self) -> float:
        return self.timeline[-1][1]


def name_parts(path: Path) -> tuple[int, int]:
    """`c8-2.csv` is concurrency 8, second repetition. A file named anything
    else is still plottable; it just becomes its own series."""
    match = LEVEL_RE.search(path.name)
    if match is None:
        return (0, 1)
    return (int(match.group(1)), int(match.group(2)))


def monotone(readings: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """A count that went backwards is a re-read of a moving index, not a
    deletion: the series is the high-water mark, the rule `rate_line` inverts."""
    top = 0.0
    points = []
    for seconds, docs in readings:
        top = max(top, docs)
        points.append((seconds, top))
    return points


def index_timeline(rows: Sequence[dict]) -> list[tuple[float, float]]:
    """The level's own start is the first point: zero documents at zero seconds,
    which is what `t_s` is measured from.

    Without it the first bucket is divided by the time since the first poll
    rather than since the level began, and a level fast enough to have passed
    the first bucket by its first reading produces no points at all — it leaves
    the chart silently, which is worse than a wrong first bucket.
    """
    readings = [(float(row["t_s"]), float(row["docs_indexed"]))
                for row in rows if row.get("docs_indexed")]
    return monotone([(0.0, 0.0)] + readings)


def handover_docs(rows: Sequence[dict]) -> float | None:
    """How big the index was when the client stopped submitting.

    Everything to the right of it was built after the last insert landed — the
    drain, which is where a build that looked fast can still be unfinished.

    The last index count seen is carried forward rather than read off the row,
    because the reading that closes the submit series carries no index count and
    would place the mark at zero documents.
    """
    indexed, at_stop, previous = 0.0, None, None
    for row in rows:
        if row.get("docs_indexed"):
            indexed = float(row["docs_indexed"])
        submitted = float(row["docs_submitted"])
        if previous is not None and submitted > previous:
            at_stop = indexed
        previous = submitted
    return at_stop


def unwatched(rows: Sequence[dict]) -> bool:
    """A `--no-index-watch` run has no index column to plot, and a run whose
    poll never succeeded has none either. Both are legal files and neither is
    a build of zero documents, so they are named and skipped."""
    return not any(row.get("docs_indexed") for row in rows)


def load(pattern: str) -> tuple[list[Level], list[str]]:
    levels, skipped = [], []
    for name in sorted(glob.glob(pattern)):
        path = Path(name)
        rows = read_csv_rows(path)
        if unwatched(rows):
            skipped.append(f"{path.name} (no index readings)")
            continue
        level = Level(path, rows)
        if level.readings < MIN_READINGS:
            skipped.append(f"{path.name} ({level.readings} readings)")
            continue
        levels.append(level)
    return levels, skipped


def by_series(levels: Sequence[Level]) -> dict[str, list[Level]]:
    """Repetitions in the order they ran, not the order the shell globbed them:
    `c8-10.csv` sorts before `c8-2.csv` as a name and after it as a run."""
    grouped: dict[str, list[Level]] = defaultdict(list)
    for level in sorted(levels, key=lambda level: level.repetition):
        grouped[level.series].append(level)
    return grouped


def series_order(grouped: dict[str, list[Level]]) -> list[str]:
    return sorted(grouped, key=lambda name: grouped[name][0].concurrency)


def grid_for(levels: Sequence[Level], step: int) -> list[float]:
    return docs_grid(step, [level.indexed for level in levels])[0]


def chosen_step(levels: Sequence[Level], asked: int) -> int:
    return docs_grid(asked, [level.indexed for level in levels])[1]


def mark_handover(axes, xs: Sequence[float], ys: Sequence[float],
                  docs: float | None, colour) -> None:
    if docs is None or not xs:
        return
    height = interpolate(xs, ys, docs)
    if height is None:
        return
    axes.plot([docs], [height], marker="|", markersize=11, color=colour,
              markeredgewidth=1.6, zorder=5)


def draw_series(axes, levels: Sequence[Level], step: int, colour, marker) -> tuple:
    """Every repetition thin, their pointwise median bold — and a single
    repetition drawn bold, because a lone thin line is a faint chart, not a
    statement about spread."""
    grid = grid_for(levels, step)
    lines = [rate_line(level.timeline, grid) for level in levels]
    for level, (xs, ys) in zip(levels, lines):
        weight = BOLD if len(levels) == 1 else THIN
        axes.plot(xs, ys, color=colour, zorder=3, **weight)
        mark_handover(axes, xs, ys, level.handover_docs, colour)
    xs, ys = pointwise_median(lines) if len(levels) > 1 else lines[0]
    if len(levels) > 1:
        axes.plot(xs, ys, color=colour, marker=marker, markersize=4,
                  markevery=max(1, len(xs) // 12), zorder=4, **BOLD)
    name = f"{levels[0].series} (n={len(levels)})"
    axes.plot([], [], color=colour, marker=marker, label=name, **BOLD)
    return (xs[-1], ys[-1], name, colour) if xs else None


def footer_lines(step: int, order: Sequence[str], skipped: Sequence[str]) -> list[str]:
    lines = [
        "x is the documents THIS level put in the index, y is how fast they went "
        f"in. Rate is recomputed on a shared grid of {step:,} documents per "
        "bucket, not read from the series' own index_docs_per_s column, so every "
        "line means the same thing whatever cadence it was polled at.",
        "A tick on a line is where the client stopped submitting: everything "
        "right of it was built after the last insert landed.",
        "Thin lines are repetitions of one concurrency, bold is their pointwise "
        "median; a concurrency measured once is drawn bold on its own.",
        "Null sink only - no engine ran, so no number here is an engine number.",
    ]
    if skipped:
        lines.append(f"SKIPPED ({len(skipped)}): {'; '.join(skipped[:6])}"
                     f"{' ...' if len(skipped) > 6 else ''}")
    return lines


def table_rows(grouped: dict[str, list[Level]], order: Sequence[str],
               step: int) -> list[list]:
    rows = []
    for name in order:
        levels = grouped[name]
        lines = [rate_line(level.timeline, grid_for(levels, step)) for level in levels]
        xs, ys = pointwise_median(lines) if len(levels) > 1 else lines[0]
        rows += [[name, len(levels), int(x), f"{y:.1f}"] for x, y in zip(xs, ys)]
    return rows


def main() -> int:
    args = parse_args()
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    levels, skipped = load(args.samples)
    if not levels:
        print(f"no series matched --samples{'; skipped: ' + '; '.join(skipped) if skipped else ''}")
        return 1

    grouped = by_series(levels)
    order = series_order(grouped)
    step = chosen_step(levels, args.grid_step)
    palette = colours(len(order))

    figure, axes = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    ends = [end for index, name in enumerate(order)
            if (end := draw_series(axes, grouped[name], step, palette[index],
                                   MARKERS[index % len(MARKERS)])) is not None]

    axes.set_xlabel("documents indexed by this level")
    axes.set_ylabel("documents indexed per second")
    axes.get_xaxis().set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}"))
    axes.set_ylim(bottom=0)
    axes.grid(True, which="major", linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    axes.set_title(args.title + (f"\n{args.subtitle}" if args.subtitle else ""),
                   fontsize=11, loc="left")
    axes.legend(fontsize=8, loc="upper right", framealpha=0.9)
    label_right_edge(axes, ends)
    figure.subplots_adjust(right=0.82, bottom=0.28)
    draw_footer(figure, footer_lines(step, order, skipped))
    figure.savefig(args.output)
    print(f"wrote {args.output}")
    if args.table:
        write_table(args.table, TABLE_COLUMNS, table_rows(grouped, order, step))
        print(f"wrote {args.table}")
    for name in skipped:
        print(f"skipped {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
