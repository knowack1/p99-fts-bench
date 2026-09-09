#!/usr/bin/env python3
"""Backup slide B6 — bulk size: where we set it, on ONE engine's axis.

x is documents per `_bulk` on a log2 axis, y is the per-run build-rate metric
at that arm's `c_sat`; the levels are the `b<batch>/` subdirectories
`tools/sweep_build_rate.sh` writes when `BATCHES` is set. Four decisions make
this chart able to damage the ceiling number as well as defend it:

- **One engine per chart, asserted rather than trusted.** At b=128 one side
  would be 128 documents in a single HTTP request and the other 128 sequential
  prepared statements, so a shared axis renders a false comparison as a
  picture. ScyllaDB is pinned at `--batch-size 1` — the CQL path has no wire
  batch — and contributes a footer sentence, never a second series.
- **The client's own ceiling is drawn on the same axis.** `--ceilings` carries
  the measured per-process operations/s ceiling per level; multiplied by the
  level it is a docs/s line, so "engine or client?" is on the axes rather than
  buried in the footer.
- **A level a gate marked is drawn hollow — never dropped, and never plotted
  as an engine number.** G7 marks a level with less than 2x headroom under the
  measured client ceiling, or none measured at all; G8 marks one whose
  `2 x c_sat` probe beat its median by more than the repetition spread, which
  means it was never shown to be a ceiling.
- **`c_sat` is owed, not guessed** — `--c-sat NAME:CONCURRENCY`, no default:
  reading whichever rung a directory happens to hold would mix the probe into
  the curve.

    python3 tools/plot_batch_ceiling.py \\
        --data-dir data/sweep-batch \\
        --c-sat opensearch-ramindex:64 \\
        --c-sat opensearch-ramindex-refresh30:96 \\
        --ceilings data/client-ceilings.json \\
        --output results/b6-batch-ceiling.png
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))

from ftsbench import (plotlib, sweep_build_rate,  # noqa: E402 (BENCH_DIR 1st)
                      verify_generator)

CHART = "B6"
CLAIM = ("A bigger _bulk buys build rate up to a plateau, and the level the "
         "campaign runs at is on that plateau rather than below it.")
DEFAULT_DATA_DIR = "data/sweep-batch"
DEFAULT_OUTPUT = "results/b6-batch-ceiling.png"
DEFAULT_METRIC = "docs_per_s_overall"
# Every unit printed here says docs/s — the y label, the per-level summary line,
# the best-level annotation — and G7 divides the plotted metric by the batch
# level to reach operations/s. Both hold for a documents/s rate and for nothing
# else, so the metric is confined to those columns of `build_report.summarize`
# rather than being allowed to turn the gate off in silence.
RATE_METRICS = frozenset({
    "docs_per_s_overall", "docs_per_s_mean", "docs_per_s_median",
    "docs_per_s_p10", "docs_per_s_p90", "docs_per_s_max"})
DEFAULT_TITLE = "Bulk size: where we set it"
DEFAULT_SUBTITLE = ("build rate at c_sat against documents per _bulk; thin "
                    "lines are repetitions, bold is their median, bars span "
                    "min..max")
BATCH_DIR_RE = re.compile(r"^b(?P<batch>\d+)$")
HEADROOM_FACTOR = 2.0
PROBE_MULTIPLE = 2
Y_HEADROOM = 1.15
CEILING_IN_VIEW = 1.6
THIN = {"linewidth": 1.0, "alpha": 0.35}
BOLD_WIDTH = 2.6
CEILING_COLOR = plotlib.ROLE_COLORS["total"]

OPERATION_NOTE = (
    "One operation is not one quantity across engines. On OpenSearch it is a "
    "wire batch: N documents, one _bulk, one request the engine sees. On "
    "ScyllaDB every row is its own prepared statement, so --batch-size was "
    "only ever a loop window inside the client, and it is pinned at 1 so that "
    "--concurrency is exactly the number of simultaneous INSERTs. The absence "
    "of a second series here is that decision, not an omission.")
ENCODE_NOTE = (
    "Encode cost is per-document work — ~16.4 us/document on the OpenSearch "
    "client against ~0.62 us on ScyllaDB — so it does NOT amortise over batch "
    "size. What amortises is the per-request HTTP cost, which is the opposite "
    "way round from the usual assumption.")
NO_CEILINGS_NOTE = (
    "no --ceilings supplied: G7 did not run, so NO level on this chart has "
    "been shown to be engine-bound rather than client-bound. Hollow markers "
    "here can only mean G8.")
SAMPLING_NOTE = (
    "the per-point cap is not the talk's operating point, and contiguous "
    "sharding at the cap is not the first N documents of the corpus")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_io_args(parser)
    add_text_args(parser)
    add_disclosure_args(parser)
    add_figure_args(parser)
    args = parser.parse_args()
    assert_metric_is_a_rate(args.metric)
    return args


def assert_metric_is_a_rate(metric: str) -> None:
    """G7 is not optional, so a metric it cannot be computed from is refused.

    The gate reads `median / batch` as operations/s against the measured client
    ceiling. Under a column that is not a documents/s rate that quotient is
    nonsense, every level clears, and the hollow marking — whose stated purpose
    is that a client-bound level is never plotted as an engine number — is off
    with nothing said. The printed docs/s unit would be wrong at the same time.
    """
    if metric in RATE_METRICS:
        return
    raise SystemExit(
        f"--metric {metric} is not a documents/s rate. G7 divides the plotted "
        f"metric by the batch level to reach operations/s and compares that "
        f"against the measured client ceiling, and every unit on this chart "
        f"reads docs/s — under any other column the gate would clear every "
        f"level in silence. Choose one of: "
        f"{', '.join(sorted(RATE_METRICS))}")


def add_io_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="directory holding b<batch>/ subdirectories of "
                             "c1-<config>-c<N>-<rep>.jsonl series")
    parser.add_argument("--c-sat", action="append", required=True,
                        metavar="NAME:CONCURRENCY",
                        help="the saturating concurrency this config's levels "
                             "were measured at, from the concurrency ladder "
                             "(repeatable, no default)")
    parser.add_argument("--metric", default=DEFAULT_METRIC,
                        help="per-run docs/s rate plotted, from "
                             "build_report.summarize; a column that is not a "
                             "docs/s rate is refused, because G7 reads it as "
                             "operations/s")
    parser.add_argument("--ceilings", default="",
                        help="JSON of measured per-process client operations/s "
                             "per batch level; without it no level can be shown "
                             "to be engine-bound")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="PNG path")
    parser.add_argument("--sidecar", default="",
                        help="sidecar JSON path (default: --output with .json)")


def add_text_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--subtitle", default=DEFAULT_SUBTITLE)
    parser.add_argument("--footer-extra", default="",
                        help="appended to the provenance footer, e.g. the repo URL")


def add_disclosure_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-preliminary-stamp", dest="stamp",
                        action="store_false",
                        help="drop the PRELIMINARY stamp; only legitimate for a "
                             "run on benchmark hardware with published tuning")
    parser.add_argument("--stamp-text", default=plotlib.PRELIMINARY_STAMP,
                        help="override the stamp wording for a run that is "
                             "preliminary for a different reason")
    parser.add_argument("--write-path-disclosure",
                        action=argparse.BooleanOptionalAction, default=False,
                        help="state the ScyllaDB base-table + CDC write "
                             "asymmetry in the footer (off by default: this "
                             "chart draws one engine)")


def add_figure_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--width", type=float, default=11.0)
    parser.add_argument("--height", type=float, default=7.5)
    parser.add_argument("--dpi", type=int, default=160)


def parse_c_sat(spec: str) -> tuple[str, int]:
    try:
        name, value = plotlib.parse_config_spec(spec)
    except ValueError:
        raise SystemExit(f"--c-sat expects NAME:CONCURRENCY, got {spec!r}")
    if not value.isdigit() or int(value) < 1:
        raise SystemExit(f"--c-sat expects NAME:CONCURRENCY, got {spec!r}")
    return name, int(value)


@dataclass(frozen=True)
class Ceilings:
    """The measured client operations/s ceiling per batch level (Phase 0).

    `loader_core_bound_at` is measured or absent, the way
    `ftsbench.client_ceilings` writes it and `ftsbench.verify_generator` reads
    it. `None` means nobody measured it, which the footer discloses — a 0.0
    default here would print as a bound of zero cores, which reads as stricter
    than any bound anyone has ever measured.
    """

    path: str
    engine: str
    measured_on: str
    ops_per_s: dict[int, float]
    loader_core_bound_at: float | None

    def at(self, batch: int) -> float | None:
        return self.ops_per_s.get(batch)

    def docs_per_s_at(self, batch: int) -> float | None:
        ops = self.at(batch)
        return None if ops is None else ops * batch


def load_ceilings(path: str, engine: str) -> Ceilings:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    ceilings = Ceilings(
        path=path, engine=str(document.get("engine", "")),
        measured_on=str(document.get("measured_on", "an unnamed box")),
        ops_per_s={int(level): float(value)
                   for level, value in document.get("ops_per_s", {}).items()},
        loader_core_bound_at=verify_generator.positive_float(
            document.get("loader_core_bound_at")))
    assert_ceilings_match_engine(ceilings, engine)
    return ceilings


def assert_ceilings_match_engine(ceilings: Ceilings, engine: str) -> None:
    """A ceiling measured against the other client prices nothing here: the two
    clients differ in what one operation costs, which is the whole axis."""
    if ceilings.engine and ceilings.engine != engine:
        raise SystemExit(
            f"--ceilings {ceilings.path} was measured for the "
            f"{ceilings.engine!r} client but this chart plots {engine!r}. The "
            "per-operation client cost differs between the two clients, so "
            "that file cannot mark a level on this axis")


@dataclass(frozen=True)
class Level:
    """One batch level of one config: its repetitions at c_sat and its probe."""

    config: str
    batch: int
    concurrency: int
    probe_concurrency: int
    runs: tuple[tuple[int, float], ...]
    probes: tuple[tuple[int, float], ...]
    files: tuple[str, ...]

    @property
    def values(self) -> list[float]:
        return [value for _, value in self.runs]

    @property
    def stats(self) -> dict[str, float]:
        return plotlib.spread(self.values)

    @property
    def median(self) -> float:
        return self.stats["median"]

    @property
    def rep_spread(self) -> float:
        return self.stats["max"] - self.stats["min"]

    @property
    def best_probe(self) -> float | None:
        return max((value for _, value in self.probes), default=None)

    def value_of(self, rep: int) -> float | None:
        return dict(self.runs).get(rep)


@dataclass(frozen=True)
class Verdict:
    """A level plus the gates that marked it, which is what draws it hollow."""

    level: Level
    reasons: tuple[str, ...]

    @property
    def hollow(self) -> bool:
        return bool(self.reasons)


def batch_directories(data_dir: str) -> list[tuple[int, str]]:
    found = []
    for entry in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, entry)
        match = BATCH_DIR_RE.match(entry)
        if match and os.path.isdir(path):
            found.append((int(match.group("batch")), path))
    return sorted(found)


def load_tree(data_dir: str) -> dict[int, list[sweep_build_rate.Point]]:
    if not os.path.isdir(data_dir):
        raise SystemExit(f"no such directory: {data_dir}")
    levels = batch_directories(data_dir)
    if not levels:
        raise SystemExit(
            f"no b<batch>/ subdirectory in {data_dir}. The batch axis writes "
            "each level to its own directory (OUT_DIR/b<batch>/) so that every "
            "summary and chart over one directory is internally single-batch; "
            "a flat directory is a concurrency ladder, which "
            "ftsbench.sweep_build_rate renders")
    return {batch: load_points(batch, path) for batch, path in levels}


def load_points(batch: int, directory: str) -> list[sweep_build_rate.Point]:
    paths = sweep_build_rate.discover_series(directory)
    points = [point for point in (sweep_build_rate.load_point(path)
                                  for path in paths) if point]
    assert_directory_batch_was_recorded(batch, points)
    return points


def assert_directory_batch_was_recorded(
        batch: int, points: list[sweep_build_rate.Point]) -> None:
    """A directory name is a label; the header is the record.

    A b64 directory holding a point recorded at batch 512 is the mislabelling
    the per-level directories exist to make impossible, and it would move a
    point along this chart's x axis without moving what it measured.

    A header that records nothing is the same defect with the evidence removed:
    the directory name is then the only thing placing the point, and reading
    that silence as agreement is what `ftsbench.sweep_build_rate` refuses one
    file earlier and what the S28 retraction turned on. So absence refuses
    here too, worded the way that module words it.
    """
    wrong = [f"{point.path} recorded "
             f"{sweep_build_rate.batch_label(point.batch_size)}"
             for point in points if point.batch_size != batch]
    if wrong:
        raise SystemExit(
            f"b{batch}/ holds series whose header does not record batch size "
            f"{batch}: {'; '.join(sorted(wrong))}. The directory places the "
            "point on the x axis and the header says what it ran with; where "
            "they disagree one of them is wrong, and where the header is "
            "silent the directory name is the only evidence for the x — fix "
            "the tree, do not plot it")


def points_by_engine(tree: dict[int, list[sweep_build_rate.Point]]
                     ) -> dict[str, list[str]]:
    engines: dict[str, list[str]] = {}
    for points in tree.values():
        for point in points:
            engines.setdefault(point.engine, []).append(point.path)
    return engines


EXAMPLES_SHOWN = 3


def engine_evidence(engine: str, paths: list[str]) -> str:
    """Enough files to act on, not every file in the tree: a message nobody
    reads to the end refuses nothing."""
    shown = ", ".join(sorted(paths)[:EXAMPLES_SHOWN])
    more = "" if len(paths) <= EXAMPLES_SHOWN else \
        f", +{len(paths) - EXAMPLES_SHOWN} more"
    return f"{engine} in {len(paths)} series ({shown}{more})"


def assert_one_engine(tree: dict[int, list[sweep_build_rate.Point]]) -> str:
    """Two engines on one batch axis is a false comparison drawn as a picture.

    At b=128 one side is 128 documents in a single HTTP request and the other
    128 sequential prepared statements. Equal x, unrelated quantities.
    """
    engines = points_by_engine(tree)
    if not engines:
        raise SystemExit("no usable c1-*.jsonl series under any b<batch>/ "
                         "directory")
    if len(engines) > 1:
        detail = "; ".join(engine_evidence(engine, paths)
                           for engine, paths in sorted(engines.items()))
        raise SystemExit(
            f"this chart draws one engine and the tree holds {len(engines)} "
            f"({detail}). Documents per _bulk is not a quantity the CQL path "
            "has — ScyllaDB runs at --batch-size 1, one prepared statement per "
            "document — so a shared batch axis would render a false comparison "
            "as a picture. Plot one engine's directories at a time")
    return next(iter(engines))


def discovered_configs(tree: dict[int, list[sweep_build_rate.Point]]) -> list[str]:
    return sorted({point.config for points in tree.values() for point in points})


def concurrencies_present(tree: dict[int, list[sweep_build_rate.Point]],
                          config: str) -> list[int]:
    return sorted({point.concurrency for points in tree.values()
                   for point in points if point.config == config})


def assert_every_config_has_c_sat(
        tree: dict[int, list[sweep_build_rate.Point]], configs: list[str],
        c_sat_of: dict[str, int]) -> None:
    """`c_sat` is owed by the concurrency ladder, and a gate that defaults is a
    guess inside the thing that exists to stop guesses."""
    missing = [config for config in configs if config not in c_sat_of]
    if not missing:
        return
    detail = "; ".join(f"{config} has rungs "
                       f"{', '.join(str(c) for c in concurrencies_present(tree, config))}"
                       for config in missing)
    raise SystemExit(
        f"no --c-sat for {', '.join(missing)}: {detail}. Each level holds N=3 "
        f"at c_sat plus one probe at {PROBE_MULTIPLE} x c_sat, so without the "
        "ladder's own c_sat this chart cannot tell the curve from the probe")


def rung(points: list[sweep_build_rate.Point], concurrency: int,
         metric: str) -> tuple[tuple[int, float], ...]:
    return tuple(sorted((point.rep, float(point.summary[metric]))
                        for point in points
                        if point.concurrency == concurrency
                        and point.summary.get(metric) is not None))


def level_for(config: str, batch: int,
              points: list[sweep_build_rate.Point], c_sat: int,
              metric: str) -> Level | None:
    mine = [point for point in points if point.config == config]
    runs = rung(mine, c_sat, metric)
    if not runs:
        report_missing_rung(config, batch, mine, c_sat)
        return None
    probe_concurrency = PROBE_MULTIPLE * c_sat
    return Level(config=config, batch=batch, concurrency=c_sat,
                 probe_concurrency=probe_concurrency, runs=runs,
                 probes=rung(mine, probe_concurrency, metric),
                 files=tuple(sorted(point.path for point in mine
                                    if point.concurrency == c_sat)))


def report_missing_rung(config: str, batch: int,
                        points: list[sweep_build_rate.Point],
                        c_sat: int) -> None:
    present = ", ".join(str(point.concurrency) for point in points) or "none"
    print(f"WARNING {config}: b{batch}/ holds no measured repetition at "
          f"c={c_sat} (rungs present: {present}) — level not drawn",
          file=sys.stderr)


def levels_for(config: str, tree: dict[int, list[sweep_build_rate.Point]],
               c_sat: int, metric: str) -> list[Level]:
    levels = [level_for(config, batch, points, c_sat, metric)
              for batch, points in sorted(tree.items())]
    return [level for level in levels if level]


def client_headroom_reason(level: Level, ceilings: Ceilings | None) -> str:
    if ceilings is None:
        return ""
    ceiling_ops = ceilings.at(level.batch)
    if ceiling_ops is None:
        return (f"G7: no client operations/s ceiling measured at batch "
                f"{level.batch} in {os.path.basename(ceilings.path)}, so this "
                f"level is not shown to be engine-bound")
    achieved = level.median / level.batch
    if achieved * HEADROOM_FACTOR > ceiling_ops:
        return (f"G7: client-bound — {achieved:,.0f} operations/s achieved "
                f"against a measured client ceiling of {ceiling_ops:,.0f} "
                f"({ceiling_ops / achieved:.2f}x, {HEADROOM_FACTOR:g}x required)")
    return ""


def pin_reason(level: Level) -> str:
    best = level.best_probe
    if best is None or best <= level.median + level.rep_spread:
        return ""
    return (f"G8: the c={level.probe_concurrency} probe read {best:,.0f} "
            f"against a c={level.concurrency} median of {level.median:,.0f} — "
            f"more than the repetition spread of {level.rep_spread:,.0f}, so "
            f"this level is a lower bound, not a ceiling")


def verdict_for(level: Level, ceilings: Ceilings | None) -> Verdict:
    reasons = (client_headroom_reason(level, ceilings), pin_reason(level))
    return Verdict(level=level, reasons=tuple(r for r in reasons if r))


def draw_repetitions(axes: Any, verdicts: list[Verdict],
                     style: dict[str, str]) -> None:
    """Every repetition as its own thin line: a median of three is robust to
    one cold repetition only if the outlier stays visible beside it."""
    for rep in sorted({rep for verdict in verdicts for rep, _ in verdict.level.runs}):
        drawn = [(verdict.level.batch, verdict.level.value_of(rep))
                 for verdict in verdicts if verdict.level.value_of(rep) is not None]
        axes.plot([batch for batch, _ in drawn], [value for _, value in drawn],
                  color=style["color"], zorder=2, **THIN)


def median_label(config: str, verdicts: list[Verdict]) -> str:
    reps = max(len(verdict.level.runs) for verdict in verdicts)
    concurrency = verdicts[0].level.concurrency
    return f"{config} — median of N={reps} at c={concurrency}"


def draw_median(axes: Any, config: str, verdicts: list[Verdict],
                style: dict[str, str]) -> None:
    batches = [verdict.level.batch for verdict in verdicts]
    medians = [verdict.level.median for verdict in verdicts]
    lows = [verdict.level.median - verdict.level.stats["min"] for verdict in verdicts]
    highs = [verdict.level.stats["max"] - verdict.level.median for verdict in verdicts]
    axes.errorbar(batches, medians, yerr=[lows, highs],
                  label=median_label(config, verdicts), marker="",
                  capsize=4, linewidth=BOLD_WIDTH, zorder=3,
                  color=style["color"], linestyle=style["linestyle"])


def draw_markers(axes: Any, verdicts: list[Verdict], color: str) -> None:
    solid = [verdict for verdict in verdicts if not verdict.hollow]
    axes.plot([verdict.level.batch for verdict in solid],
              [verdict.level.median for verdict in solid], linestyle="none",
              marker="o", markersize=8, color=color, zorder=4)
    marked = [verdict for verdict in verdicts if verdict.hollow]
    axes.plot([verdict.level.batch for verdict in marked],
              [verdict.level.median for verdict in marked], linestyle="none",
              marker="o", markersize=11, markerfacecolor="white",
              markeredgewidth=1.8, markeredgecolor=color, zorder=4)


def label_alignment(batch: int, batches: list[int]) -> tuple[str, int]:
    """Anchored inwards at the axis edges: a centred label on the last level
    hangs off the canvas, where it discloses nothing."""
    if batch == batches[-1]:
        return "right", -6
    if batch == batches[0]:
        return "left", 6
    return "center", 0


def label_best(axes: Any, config: str, verdicts: list[Verdict],
               style: dict[str, str], index: int, batches: list[int]) -> None:
    best = max(verdicts, key=lambda verdict: verdict.level.median)
    alignment, nudge = label_alignment(best.level.batch, batches)
    axes.annotate(f"{config} — best {best.level.median / 1000:.1f}k docs/s "
                  f"at b={best.level.batch}",
                  xy=(best.level.batch, best.level.median),
                  xytext=(nudge, 14 + 20 * index), textcoords="offset points",
                  ha=alignment, fontsize=9.5, color=style["color"],
                  weight="bold")


def draw_config(axes: Any, config: str, verdicts: list[Verdict], index: int,
                batches: list[int]) -> None:
    style = plotlib.style_for(config, index)
    draw_repetitions(axes, verdicts, style)
    draw_median(axes, config, verdicts, style)
    draw_markers(axes, verdicts, style["color"])
    label_best(axes, config, verdicts, style, index, batches)


def draw_client_ceiling(axes: Any, batches: list[int],
                        ceilings: Ceilings) -> None:
    """The client ceiling in the y axis's own units, so a level that is really
    the loader's number cannot look like the engine's."""
    drawn = [(batch, ceilings.docs_per_s_at(batch)) for batch in batches
             if ceilings.docs_per_s_at(batch) is not None]
    if not drawn:
        return
    axes.plot([batch for batch, _ in drawn], [value for _, value in drawn],
              linestyle="--", linewidth=1.5, marker="v", markersize=5,
              color=CEILING_COLOR, zorder=2,
              label=f"client operation ceiling x batch ({ceilings.measured_on})")


def all_batches(plans: dict[str, list[Verdict]]) -> list[int]:
    return sorted({verdict.level.batch for verdicts in plans.values()
                   for verdict in verdicts})


def ceiling_values(plans: dict[str, list[Verdict]],
                   ceilings: Ceilings) -> list[float]:
    values = [ceilings.docs_per_s_at(batch) for batch in all_batches(plans)]
    return [value for value in values if value is not None]


def y_top(plans: dict[str, list[Verdict]], ceilings: Ceilings | None) -> float:
    """The axis is scaled to the measured rates.

    A client ceiling several times the plotted rate is the good case — ample
    headroom — and letting it set the top would squash every measured point
    into the bottom fifth of the chart. Ceilings above the view run off the top
    edge, which reads as "above this range", and the footer carries the
    numbers.
    """
    top = max(verdict.level.stats["max"] for verdicts in plans.values()
              for verdict in verdicts)
    in_view = [] if ceilings is None else [
        value for value in ceiling_values(plans, ceilings)
        if value <= top * CEILING_IN_VIEW]
    return max([top, *in_view]) * Y_HEADROOM


def offscale_batches(plans: dict[str, list[Verdict]],
                     ceilings: Ceilings | None, top: float) -> list[int]:
    if ceilings is None:
        return []
    return [batch for batch in all_batches(plans)
            if (ceilings.docs_per_s_at(batch) or 0.0) > top]


def finish_axes(axes: Any, args: argparse.Namespace, batches: list[int],
                top: float) -> None:
    axes.set_xscale("log", base=2)
    axes.set_xticks(batches)
    axes.set_xticklabels([str(batch) for batch in batches])
    axes.set_ylim(0, top)
    axes.legend(loc="lower right", fontsize=9, framealpha=0.92)
    plotlib.frame(axes, args,
                  "documents per _bulk (--batch-size), log2 — one engine only",
                  f"build rate at c_sat ({args.metric}, docs/s)")


def render(args: argparse.Namespace, plans: dict[str, list[Verdict]],
           ceilings: Ceilings | None) -> Any:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    batches = all_batches(plans)
    for index, config in enumerate(sorted(plans)):
        draw_config(axes, config, plans[config], index, batches)
    if ceilings is not None:
        draw_client_ceiling(axes, batches, ceilings)
    finish_axes(axes, args, batches, y_top(plans, ceilings))
    return figure


def in_flight_note(plans: dict[str, list[Verdict]]) -> str:
    """Requests against documents in flight, at this chart's own top level."""
    verdicts = [verdict for series in plans.values() for verdict in series]
    top = max(verdicts, key=lambda verdict: verdict.level.batch).level
    return (f"requests against documents in flight: at c={top.concurrency} and "
            f"batch {top.batch} the plotted engine holds {top.concurrency} "
            f"requests carrying {top.concurrency * top.batch:,} documents; at "
            f"batch 1 those same {top.concurrency} requests carry "
            f"{top.concurrency} documents, which is the ScyllaDB shape")


def ceiling_note(plans: dict[str, list[Verdict]],
                 ceilings: Ceilings | None) -> str:
    if ceilings is None:
        return NO_CEILINGS_NOTE
    levels = ", ".join(f"b{batch}: {ops:,.0f} ops/s"
                       for batch, ops in sorted(ceilings.ops_per_s.items()))
    return (f"measured client operation ceiling per level, on "
            f"{ceilings.measured_on} — {levels}; a level clears at "
            f"{HEADROOM_FACTOR:g}x below its own level's ceiling, "
            + core_bound_clause(ceilings)
            + offscale_note(plans, ceilings))


def core_bound_clause(ceilings: Ceilings) -> str:
    """Phase 0 owes the per-core bound and may not have measured it yet, and a
    clause about a constant nobody measured is a fabricated measurement in the
    artifact whose whole job is auditability."""
    if ceilings.loader_core_bound_at is None:
        return ("and the per-core loader bound was not measured, so G7 here "
                "rests on the operations/s margin alone")
    return f"with the loader under {ceilings.loader_core_bound_at:g} of a core"


def offscale_note(plans: dict[str, list[Verdict]],
                  ceilings: Ceilings) -> str:
    offscale = offscale_batches(plans, ceilings, y_top(plans, ceilings))
    if not offscale:
        return ""
    return (". The dashed ceiling leaves the top of the axis at "
            + ", ".join(f"b{batch}" for batch in offscale)
            + ": the axis is scaled to the measured rates, not to the headroom")


def marked_note(plans: dict[str, list[Verdict]]) -> str:
    marked = [f"{config} b{verdict.level.batch} [{'; '.join(verdict.reasons)}]"
              for config, verdicts in sorted(plans.items())
              for verdict in verdicts if verdict.hollow]
    if not marked:
        return "no level was marked by G7 or G8: every marker is filled"
    return ("hollow markers are levels a gate marked, drawn as lower bounds "
            "rather than dropped — " + "; ".join(marked))


def cap_note(series: list[plotlib.ConfigSeries]) -> str:
    caps = sorted({run.header.get("max_docs") for config in series
                   for run in config.runs} - {None})
    if not caps:
        return SAMPLING_NOTE
    return (f"per-point cap {', '.join(f'{cap:,}' for cap in caps)} documents; "
            + SAMPLING_NOTE)


def footer_notes(plans: dict[str, list[Verdict]],
                 series: list[plotlib.ConfigSeries],
                 ceilings: Ceilings | None) -> list[str]:
    return [OPERATION_NOTE, in_flight_note(plans), ceiling_note(plans, ceilings),
            marked_note(plans), ENCODE_NOTE, cap_note(series)]


def sorted_points(points: list[sweep_build_rate.Point]
                  ) -> list[sweep_build_rate.Point]:
    return sorted(points, key=lambda point: point.path)


def as_runs(points: list[sweep_build_rate.Point]) -> list[plotlib.Run]:
    return [plotlib.Run(path=point.path, header=point.header, records=[])
            for point in sorted_points(points)]


def points_at_c_sat(config: str, tree: dict[int, list[sweep_build_rate.Point]],
                    c_sat: int, metric: str
                    ) -> dict[int, list[sweep_build_rate.Point]]:
    return {batch: [point for point in points
                    if point.config == config and point.concurrency == c_sat
                    and point.summary.get(metric) is not None]
            for batch, points in sorted(tree.items())}


def config_series(config: str, tree: dict[int, list[sweep_build_rate.Point]],
                  c_sat: int, metric: str) -> plotlib.ConfigSeries:
    """The provenance view of one config, so the footer, the corpus agreement
    check and the sidecar all come from plotlib rather than from here.

    Provenance is the reference level's — the largest batch, which is the value
    the campaign itself runs at — so the footer's N is the repetitions of one
    level rather than the sum over the axis. Corpus agreement is checked across
    every level, because one smoke repetition anywhere on the axis flattens the
    curve wherever it lands.
    """
    by_level = points_at_c_sat(config, tree, c_sat, metric)
    plotlib.assert_one_measurement(
        config, as_runs([point for points in by_level.values() for point in points]))
    reference = by_level[max(batch for batch, points in by_level.items() if points)]
    metrics = [float(point.summary[metric]) for point in sorted_points(reference)]
    return plotlib.ConfigSeries(name=config, runs=as_runs(reference),
                                metrics=metrics,
                                chosen_index=plotlib.median_index(metrics))


def level_sidecar(verdict: Verdict) -> dict[str, Any]:
    level = verdict.level
    return {
        "batch_size": level.batch,
        "concurrency": level.concurrency,
        "repetitions": len(level.runs),
        "values": [value for _, value in level.runs],
        "spread": level.stats,
        "probe_concurrency": level.probe_concurrency,
        "probe_values": [value for _, value in level.probes],
        "marked_by": list(verdict.reasons),
        "is_lower_bound": verdict.hollow,
        "files": list(level.files),
    }


def ceilings_sidecar(ceilings: Ceilings | None) -> dict[str, Any]:
    if ceilings is None:
        return {"source": "", "note": NO_CEILINGS_NOTE}
    return {"source": ceilings.path, "engine": ceilings.engine,
            "measured_on": ceilings.measured_on,
            "ops_per_s": {str(batch): ops
                          for batch, ops in sorted(ceilings.ops_per_s.items())},
            "loader_core_bound_at": ceilings.loader_core_bound_at,
            "headroom_factor": HEADROOM_FACTOR}


def sidecar_document(args: argparse.Namespace, engine: str,
                     series: list[plotlib.ConfigSeries],
                     plans: dict[str, list[Verdict]],
                     ceilings: Ceilings | None,
                     notes: list[str]) -> dict[str, Any]:
    return {
        "chart": CHART,
        "title": args.title,
        "subtitle": args.subtitle,
        "png": args.output,
        "command": " ".join(sys.argv),
        "claim": CLAIM,
        "claim_status": plotlib.CLAIM_UNASSESSED,
        "engine": engine,
        "metric": args.metric,
        "x_axis": "documents per _bulk (--batch-size), log2",
        "y_axis": f"{args.metric} at c_sat",
        "run_selection": "every repetition at c_sat is drawn thin and the bold "
                         "line is the per-level median; repetitions are never "
                         "averaged",
        "preliminary": bool(args.stamp),
        "preliminary_stamp": args.stamp_text if args.stamp else "",
        "chart_notes": notes,
        "client_ceilings": ceilings_sidecar(ceilings),
        "levels": {config: [level_sidecar(verdict) for verdict in verdicts]
                   for config, verdicts in sorted(plans.items())},
        "configs": {config.name: plotlib.config_sidecar(config, args.metric)
                    for config in series},
    }


def print_summary(plans: dict[str, list[Verdict]]) -> None:
    for config, verdicts in sorted(plans.items()):
        for verdict in verdicts:
            mark = "HOLLOW" if verdict.hollow else "solid "
            print(f"{config:<32} b{verdict.level.batch:<4} {mark} "
                  f"median={verdict.level.median:,.0f} docs/s  "
                  f"N={len(verdict.level.runs)}", file=sys.stderr)


def plan_configs(tree: dict[int, list[sweep_build_rate.Point]],
                 c_sat_of: dict[str, int], args: argparse.Namespace,
                 ceilings: Ceilings | None) -> dict[str, list[Verdict]]:
    plans = {}
    for config in discovered_configs(tree):
        levels = levels_for(config, tree, c_sat_of[config], args.metric)
        if levels:
            plans[config] = [verdict_for(level, ceilings) for level in levels]
    return plans


def write_outputs(args: argparse.Namespace, engine: str,
                  series: list[plotlib.ConfigSeries],
                  plans: dict[str, list[Verdict]], ceilings: Ceilings | None,
                  notes: list[str]) -> None:
    plotlib.ensure_parent_dir(args.output)
    plotlib.finish_figure(render(args, plans, ceilings), args, series, notes)
    sidecar = plotlib.sidecar_path(args)
    plotlib.write_sidecar(sidecar, sidecar_document(args, engine, series, plans,
                                                    ceilings, notes))
    print_summary(plans)
    print(f"wrote {args.output} and {sidecar}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    tree = load_tree(args.data_dir)
    engine = assert_one_engine(tree)
    c_sat_of = dict(parse_c_sat(spec) for spec in args.c_sat)
    assert_every_config_has_c_sat(tree, discovered_configs(tree), c_sat_of)
    ceilings = load_ceilings(args.ceilings, engine) if args.ceilings else None
    plans = plan_configs(tree, c_sat_of, args, ceilings)
    if not plans:
        print("error: no config produced a measured level; nothing to plot",
              file=sys.stderr)
        return 1
    series = [config_series(config, tree, c_sat_of[config], args.metric)
              for config in sorted(plans)]
    write_outputs(args, engine, series, plans, ceilings,
                  footer_notes(plans, series, ceilings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
