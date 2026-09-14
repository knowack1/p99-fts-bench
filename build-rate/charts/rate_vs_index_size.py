#!/usr/bin/env python3
"""The build rate against the index each engine has already built.

X is the documents this build put in the index, Y is how fast they went in, and
**one series is one index build**: the ScyllaDB build at some concurrency, and
the OpenSearch build at that same concurrency for each batch size, on one axis.
It answers what a whole-level average cannot — a client racing ahead of its
index, a stall, and the tail after the client stops while the index is still
draining.

    build-rate/charts/rate_vs_index_size.py \\
        --scylla     '<R>/scylla/samples/*/c32-*.csv' \\
        --opensearch '<R>/opensearch/samples/*/c32-b*-*.csv' \\
        --output     '<R>/build-rate-vs-index-size.png' \\
        --table      '<R>/build-rate-vs-index-size.csv'

Which builds land on the chart is chosen by the **glob** — `c32-*` takes the
`c=32` slice of every ladder — because "every build we ran" is one line per
concurrency per batch level and stops being readable somewhere around a dozen.

Sibling of `tools/plot_build_growth.py`, whose grid, timeline, riser floor and
drawing this reuses wholesale so the two charts mean the same thing. What it
adds is the engine: both halves write `c32-1.csv`, so without a prefix on the
series name the ScyllaDB build and the OpenSearch one merge into a single
averaged line that is neither.

X means the same thing on both halves — documents this build made searchable.
**Y does not.** On ScyllaDB it is the vector-store's Tantivy build publishing
continuously; on OpenSearch it is refresh-gated visibility, which climbs in
steps, and the bucket is widened to one riser so those steps are not read high
by the refresh-to-poll ratio.
"""
from __future__ import annotations

import argparse
import glob
import sys
from collections import defaultdict
from pathlib import Path
from typing import Sequence

BENCH_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "tools"))

import plot_build_growth as growth  # noqa: E402
import plot_harness_grid as grid  # noqa: E402
from harness_charts import (MARKERS, colours, draw_footer,  # noqa: E402
                            label_right_edge, read_csv_rows, write_table)

SCYLLA_ENGINE = grid.SCYLLA_ENGINE
OPENSEARCH_ENGINE = grid.OPENSEARCH_ENGINE
SCYLLA_COLOR = grid.SCYLLA_COLOR
TABLE_COLUMNS = growth.TABLE_COLUMNS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scylla", default="",
                        help="glob of scyllarate series CSVs, e.g. '<dir>/*/c32-*.csv'")
    parser.add_argument("--opensearch", default="",
                        help="glob of osrate series CSVs, e.g. '<dir>/*/c32-b*-*.csv'")
    parser.add_argument("--output", required=True, help="PNG path")
    parser.add_argument("--table", default="", help="also write every plotted point as CSV")
    parser.add_argument("--grid-step", type=int, default=0,
                        help="documents per bucket; 0 picks one from the corpus size")
    parser.add_argument("--title", default="Build rate as the index grows")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)
    return parser.parse_args()


def name_of(engine: str, concurrency: int, batch_size: int) -> str:
    """What a build is called on the chart.

    The engine leads because that is the coarsest thing telling two lines apart
    here, and it is omitted when there is none so a single-engine run reads the
    way `tools/plot_build_growth.py` has always read.
    """
    parts = [engine] if engine else []
    parts.append(f"c={concurrency}")
    if batch_size:
        parts.append(f"batch={batch_size}")
    return " ".join(parts)


class Level(growth.Level):
    """One build, tagged with the engine that performed it."""

    def __init__(self, path: Path, rows: Sequence[dict], engine: str) -> None:
        super().__init__(path, rows)
        self.engine = engine

    @property
    def series(self) -> str:
        return name_of(self.engine, self.concurrency, self.batch_size)


def skip_reason(path: Path, rows: Sequence[dict]) -> str:
    """Why this file is not a build that can be drawn, or an empty string.

    The three rules are `plot_build_growth`'s, imported rather than restated:
    an index nobody watched, an index that published nothing, and a build too
    short to have a shape.
    """
    if growth.unwatched(rows):
        return f"{path.name} (no index readings)"
    if growth.published_nothing(rows):
        return f"{path.name} (nothing became searchable)"
    return ""


def load(pattern: str, engine: str = "") -> tuple[list[Level], list[str]]:
    levels, skipped = [], []
    for name in sorted(glob.glob(pattern)):
        path = Path(name)
        rows = read_csv_rows(path)
        reason = skip_reason(path, rows)
        if reason:
            skipped.append(reason)
            continue
        level = Level(path, rows, engine)
        if level.readings < growth.MIN_READINGS:
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
    """ScyllaDB first, then OpenSearch by ascending batch size and concurrency.

    ScyllaDB leads so it keeps the pinned blue on both charts of a run: a reader
    who learned that colour on the concurrency chart has to find it here.
    """
    def key(name: str) -> tuple:
        level = grouped[name][0]
        return (0 if level.engine == SCYLLA_ENGINE else 1,
                level.batch_size, level.concurrency)
    return sorted(grouped, key=key)


def colour_for(grouped: dict[str, list[Level]], order: Sequence[str]) -> list:
    warm = colours(len([name for name in order
                        if grouped[name][0].engine != SCYLLA_ENGINE]))
    palette, warm_index = [], 0
    for name in order:
        if grouped[name][0].engine == SCYLLA_ENGINE:
            palette.append(SCYLLA_COLOR)
        else:
            palette.append(warm[warm_index])
            warm_index += 1
    return palette


def footer_lines(step: int, skipped: Sequence[str], floored: bool,
                 both_engines: bool) -> list[str]:
    lines = [
        "x is the documents THIS build put in the index, y is how fast they went "
        f"in. Rate is recomputed on a shared grid of {step:,} documents per "
        "bucket, not read from the series' own index_docs_per_s column, so every "
        "line means the same thing whatever cadence it was polled at.",
    ]
    if floored:
        lines.append(
            f"The bucket is one riser wide ({step:,} documents) because a series "
            "here climbs in steps rather than continuously: an index whose "
            "searchable count only advances at a refresh. A finer bucket would "
            "land inside a jump and read the rate high by the ratio between the "
            "refresh and the poll, and the flats between jumps would disappear.")
    lines += [
        "A tick on a line is where the client stopped submitting: everything "
        "right of it was built after the last insert landed.",
        "Thin lines are repetitions of one build, bold is their pointwise "
        "median; a build measured once is drawn bold on its own.",
    ]
    if both_engines:
        lines.append(
            "x means the same thing on both halves; y does NOT. On ScyllaDB it "
            "is the vector-store's Tantivy build publishing continuously, on "
            "OpenSearch it is refresh-gated visibility. Compare the SHAPE of a "
            "line against its own budget, not one engine's height against the "
            "other's.")
    lines.append(
        "Laptop, shared box, docker/.env laptop-simulation caps: NOT QUOTABLE.")
    if skipped:
        lines.append(f"SKIPPED ({len(skipped)}): {'; '.join(skipped[:6])}"
                     f"{' ...' if len(skipped) > 6 else ''}")
    return lines


def main() -> int:
    args = parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    levels, skipped = [], []
    for pattern, engine in ((args.scylla, SCYLLA_ENGINE),
                            (args.opensearch, OPENSEARCH_ENGINE)):
        if not pattern:
            continue
        found, missed = load(pattern, engine)
        levels += found
        skipped += missed
    if not levels:
        print("no series matched --scylla / --opensearch"
              + ("; skipped: " + "; ".join(skipped) if skipped else ""))
        return 1

    grouped = by_series(levels)
    order = series_order(grouped)
    step = growth.chosen_step(levels, args.grid_step)
    floored = growth.riser_floor(levels) >= step > 0
    palette = colour_for(grouped, order)
    engines = {level.engine for level in levels}

    figure, axes = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    ends = [end for index, name in enumerate(order)
            if (end := growth.draw_series(axes, grouped[name], step,
                                          palette[index],
                                          MARKERS[index % len(MARKERS)])) is not None]

    axes.set_xlabel("documents in the index")
    axes.set_ylabel("documents indexed per second")
    axes.get_xaxis().set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}"))
    axes.set_ylim(bottom=0)
    axes.grid(True, which="major", linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    axes.set_title(args.title + (f"\n{args.subtitle}" if args.subtitle else ""),
                   fontsize=11, loc="left")
    axes.legend(fontsize=8, loc="upper right", framealpha=0.9)
    label_right_edge(axes, ends)

    figure.subplots_adjust(right=0.82, bottom=0.32)
    draw_footer(figure, footer_lines(step, skipped, floored, len(engines) > 1))
    figure.savefig(args.output, dpi=args.dpi)
    print(f"wrote {args.output}  ({len(order)} series)")

    if args.table:
        write_table(args.table, TABLE_COLUMNS,
                    growth.table_rows(grouped, order, step))
        print(f"wrote {args.table}")
    for name in skipped:
        print(f"skipped {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
