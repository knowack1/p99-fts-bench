"""Render chart C2 — time until the whole corpus is searchable.

C2 is **derived from the C1 sample series**, not from an artifact of its own:
the harness already records `index_status` and `docs_searchable` on every
build-monitor tick, and measuring the same quantity a second time with a second
tool would be two chances to disagree.

The two engines terminate on different signals, and that is a property of the
engines rather than a fudge:

- ScyllaDB: the first sample whose `index_status` is `SERVING` — the
  vector-store publishes the index as a unit once the scan or CDC tail lands.
- OpenSearch: the first sample whose `docs_searchable` reaches the corpus
  count — documents become searchable progressively as segments refresh, so
  "serving" is true almost immediately and would be a meaningless bar.

    python3 -m ftsbench.plot_c2 \\
        --config opensearch:data/c1-opensearch-*.jsonl \\
        --config scylla-bootstrap:data/c1-scylla-bootstrap-*.jsonl \\
        --output results/c2.png

A repetition that never satisfied its condition is **not** dropped: it is
reported as unresolved on the chart, because a build that never became fully
searchable is the strongest possible C2 result.

Measured on the frozen simplewiki series, the ScyllaDB CDC tail publishes an
`index_status` of `SERVING` at the first sample, with zero documents in the
index — the status describes the index, not the corpus. The contract condition
is still what the solid bar reports, but a bar resolved while `docs_searchable`
was below the corpus count is drawn as a **lower bound** and paired with a
hatched bar for the moment every document was actually searchable. Reporting
0.002 s as "time until searchable" would have been the alternative.
"""
import argparse
import functools
import sys
from dataclasses import dataclass
from typing import Any

from . import plotlib

CHART = "C2"
RECORD = "sample"
DEFAULT_OUTPUT = "results/c2.png"
DEFAULT_TITLE = "Time until the corpus is fully searchable"
DEFAULT_SUBTITLE = ("lower is better; whiskers are the observed range over "
                    "repetitions, dots are the individual repetitions")
SERVING = "SERVING"
BAR_WIDTH = 0.45
METRIC_NAME = "time_to_searchable_s"


@dataclass(frozen=True)
class Outcome:
    """What one config's repetitions did, resolved and unresolved alike."""

    name: str
    seconds: float | None
    condition: str
    target_docs: int
    docs_at_condition: int
    all_docs_seconds: float | None
    resolved: list[float]
    unresolved_files: list[str]

    @property
    def lower_bound(self) -> bool:
        """The condition fired before the corpus was in the index."""
        return self.seconds is not None and self.docs_at_condition < self.target_docs


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(
        CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE, DEFAULT_SUBTITLE,
        disclose_write_path=True, width=9.0, height=6.0)
    parser.add_argument("--corpus-docs", type=int, default=0,
                        help="documents the corpus holds; 0 derives it from the "
                             "header's max_docs, then from the series' own maximum")
    return parser.parse_args()


def uses_serving_status(engine: str) -> bool:
    return plotlib.is_scylla(engine)


def corpus_target(run: plotlib.Run, override: int) -> int:
    if override > 0:
        return override
    declared = int(run.header.get("max_docs") or 0)
    if declared > 0:
        return declared
    return max((int(item.get("docs_indexed") or 0) for item in run.records),
               default=0)


def first_serving(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in samples
                 if str(item.get("index_status") or "").upper() == SERVING), None)


def first_fully_searchable(samples: list[dict[str, Any]],
                           target: int) -> dict[str, Any] | None:
    if target <= 0:
        return None
    return next((item for item in samples
                 if int(item.get("docs_searchable") or 0) >= target), None)


def searchable_sample(run: plotlib.Run, override: int) -> dict[str, Any] | None:
    target = corpus_target(run, override)
    if uses_serving_status(run.engine):
        return first_serving(run.records)
    return first_fully_searchable(run.records, target)


def condition_for(run: plotlib.Run) -> str:
    if uses_serving_status(run.engine):
        return "first sample with index_status == SERVING"
    return "first sample with docs_searchable >= corpus count"


def time_to_searchable(run: plotlib.Run, override: int = 0) -> float:
    reached = searchable_sample(run, override)
    if reached is None:
        return plotlib.UNRESOLVED
    return float(reached.get("t_elapsed_s") or 0.0)


def docs_at(sample: dict[str, Any] | None) -> int:
    return int((sample or {}).get("docs_searchable") or 0)


def all_docs_searchable_s(run: plotlib.Run, target: int) -> float | None:
    reached = first_fully_searchable(run.records, target)
    return None if reached is None else float(reached.get("t_elapsed_s") or 0.0)


def outcome_for(config: plotlib.ConfigSeries, override: int) -> Outcome:
    resolved = [value for value in config.metrics if value != plotlib.UNRESOLVED]
    unresolved = [run.path for run, value in zip(config.runs, config.metrics)
                  if value == plotlib.UNRESOLVED]
    chosen, run = config.chosen_metric, config.chosen
    target = corpus_target(run, override)
    return Outcome(name=config.name,
                   seconds=None if chosen == plotlib.UNRESOLVED else chosen,
                   condition=condition_for(run), target_docs=target,
                   docs_at_condition=docs_at(searchable_sample(run, override)),
                   all_docs_seconds=all_docs_searchable_s(run, target),
                   resolved=resolved, unresolved_files=unresolved)


def draw_bar(axes: Any, position: int, outcome: Outcome, style: dict[str, str]) -> None:
    axes.bar(position, outcome.seconds, width=BAR_WIDTH, color=style["color"],
             alpha=0.85, zorder=2)
    axes.annotate(f"{outcome.seconds:,.1f} s", (position, outcome.seconds),
                  textcoords="offset points", xytext=(0, 6), ha="center", fontsize=9)


def draw_range(axes: Any, position: int, outcome: Outcome) -> None:
    if len(outcome.resolved) < 2:
        return
    low, high = min(outcome.resolved), max(outcome.resolved)
    axes.vlines(position, low, high, color="#333333", linewidth=1.2, zorder=3)
    axes.scatter([position] * len(outcome.resolved), outcome.resolved, s=18,
                 facecolors="none", edgecolors="#333333", linewidths=0.9, zorder=4)


def draw_unresolved(axes: Any, position: int, outcome: Outcome) -> None:
    axes.annotate(f"never fully searchable\nin {len(outcome.unresolved_files)} rep(s)",
                  (position, 0.0), textcoords="offset points", xytext=(0, 10),
                  ha="center", fontsize=8.5, color="#8c1d13", weight="bold")


def draw_lower_bound(axes: Any, position: int, outcome: Outcome) -> None:
    if outcome.all_docs_seconds is None:
        return
    axes.bar(position, outcome.all_docs_seconds, width=BAR_WIDTH, facecolor="none",
             edgecolor="#8c1d13", hatch="//", linewidth=1.1, zorder=1)
    axes.annotate(f"all {outcome.target_docs:,} docs searchable\n"
                  f"at {outcome.all_docs_seconds:,.1f} s",
                  (position, outcome.all_docs_seconds), textcoords="offset points",
                  xytext=(0, 6), ha="center", fontsize=8, color="#8c1d13")


def draw_outcome(axes: Any, position: int, outcome: Outcome,
                 style: dict[str, str]) -> None:
    if outcome.seconds is None:
        draw_unresolved(axes, position, outcome)
        return
    if outcome.lower_bound:
        draw_lower_bound(axes, position, outcome)
    draw_bar(axes, position, outcome, style)
    draw_range(axes, position, outcome)


def unresolved_note(outcomes: list[Outcome]) -> str:
    affected = [f"{outcome.name} ({len(outcome.unresolved_files)})"
                for outcome in outcomes if outcome.unresolved_files]
    if not affected:
        return ""
    return ("repetitions that never became fully searchable, excluded from the "
            "median: " + ", ".join(affected))


def lower_bound_note(outcomes: list[Outcome]) -> str:
    affected = [f"{outcome.name} (condition met with {outcome.docs_at_condition:,} "
                f"of {outcome.target_docs:,} docs searchable)"
                for outcome in outcomes if outcome.lower_bound]
    if not affected:
        return ""
    return ("LOWER BOUND — the solid bar is when the index reported its status, not "
            "when the corpus was searchable: " + ", ".join(affected) +
            "; the hatched bar is when every document was searchable")


def condition_note(outcomes: list[Outcome]) -> str:
    return "termination condition — " + "; ".join(
        f"{outcome.name}: {outcome.condition} (corpus {outcome.target_docs:,} docs)"
        for outcome in outcomes)


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    outcomes = [outcome_for(config, args.corpus_docs) for config in configs]
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    for position, (config, outcome) in enumerate(zip(configs, outcomes)):
        draw_outcome(axes, position, outcome, plotlib.style_for(config.name, position))
    axes.set_xticks(range(len(outcomes)))
    axes.set_xticklabels([outcome.name for outcome in outcomes])
    axes.set_ylim(bottom=0)
    plotlib.frame(axes, args, "", "seconds from monitor start")
    return figure, extras(outcomes)


def extras(outcomes: list[Outcome]) -> dict[str, Any]:
    notes = [condition_note(outcomes), lower_bound_note(outcomes),
             unresolved_note(outcomes), plotlib.SCYLLA_RATE_NOTE]
    return {
        "metric_name": METRIC_NAME,
        "notes": [note for note in notes if note],
        "y_axis": "seconds from monitor start",
        "derivation": "derived from the C1 sample series; no separate measurement",
        "per_config": {outcome.name: config_extras(outcome) for outcome in outcomes},
    }


def config_extras(outcome: Outcome) -> dict[str, Any]:
    return {
        "time_to_searchable_s": outcome.seconds,
        "termination_condition": outcome.condition,
        "corpus_docs": outcome.target_docs,
        "docs_searchable_at_condition": outcome.docs_at_condition,
        "is_lower_bound": outcome.lower_bound,
        "time_to_all_docs_searchable_s": outcome.all_docs_seconds,
        "resolved_repetitions": len(outcome.resolved),
        "resolved_spread_s": plotlib.spread(outcome.resolved),
        "unresolved_repetition_files": outcome.unresolved_files,
    }


def main() -> int:
    args = parse_args()
    metric = functools.partial(time_to_searchable, override=args.corpus_docs)
    return plotlib.emit(args, RECORD, metric, plot)


if __name__ == "__main__":
    sys.exit(main())
