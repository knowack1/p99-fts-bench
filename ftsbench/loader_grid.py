"""One arm's line, from the points `client_ceilings` has already reduced.

Shared by `plot_loader_capability` and `plot_loader_cpu` so the two charts
cannot disagree about which points make an arm, which repetition is drawn, or
what an arm is called. Neither chart reduces anything itself: the campaign's
artifacts are named for `client_ceilings.POINT_RE` precisely so its
`point_result` — which already knows that a point's wall is its slowest shard's,
and that a rate divides by ok_docs rather than docs — stays the only place that
arithmetic lives.

**Colour carries the ENGINE, the worker count carries everything else.** Six
lines want six hues, and six hues do not fit: three steps inside one hue family
cannot all sit in the lightness band and still clear the normal-vision floor,
which the dataviz validator fails them for (measured: a 3-step warm ramp puts
two of its steps outside L 0.43-0.77). So the two engine hues the deck already
uses stay as they are — they pass every check on the all-pairs list, worst ΔE
19.9 protanopic and 27.8 normal — and N is encoded four ways that are not
colour: line style, marker, line width, and a direct label. That is past the
"never colour alone" rule, and it keeps OpenSearch warm and ScyllaDB cool, which
the rest of the deck relies on.
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import client_ceilings, plotlib

# From plotlib.CONFIG_STYLES, unchanged: the families the deck already reads as
# "the JVM one" and "the ScyllaDB one".
ENGINE_COLORS = {"opensearch": "#c2432b", "scylladb": "#2b6cb0"}
ENGINE_FALLBACK = "#4a5568"
# Four encodings per worker count, none of them hue. The widths descend so that
# where two arms overlap the sparser style stays visible on top.
WORKER_STYLES = {
    4: {"linestyle": "-", "marker": "o", "linewidth": 2.6},
    6: {"linestyle": "--", "marker": "s", "linewidth": 2.1},
    8: {"linestyle": (0, (1, 1.4)), "marker": "^", "linewidth": 1.8},
}
WORKER_FALLBACK = {"linestyle": "-.", "marker": "D", "linewidth": 1.6}
THIN = {"linewidth": 0.9, "alpha": 0.32}
Metric = Callable[["client_ceilings.PointResult"], "float | None"]


def docs_per_s(point: client_ceilings.PointResult) -> float | None:
    return point.docs_per_s or None


def ops_per_s(point: client_ceilings.PointResult) -> float | None:
    return point.ops_per_s or None


def box_cores_p50(point: client_ceilings.PointResult) -> float | None:
    return point.cost.box_cores_p50 if point.cost else None


def box_cores_max(point: client_ceilings.PointResult) -> float | None:
    return point.cost.box_cores_max if point.cost else None


def loaders_cores_p50(point: client_ceilings.PointResult) -> float | None:
    return point.cost.loaders_cores_p50 if point.cost else None


def busiest_thread_cores(point: client_ceilings.PointResult) -> float | None:
    return point.cost.busiest_thread_cores_max if point.cost else None


METRICS: dict[str, Metric] = {
    "docs_per_s": docs_per_s,
    "ops_per_s": ops_per_s,
    "box_cores_p50": box_cores_p50,
    "box_cores_max": box_cores_max,
    "loaders_cores_p50": loaders_cores_p50,
    "busiest_thread_cores": busiest_thread_cores,
}


def shard_zero_name(key: client_ceilings.PointKey) -> str:
    """The point's first latency artifact, by the campaign's naming rule.

    Rebuilt from the key rather than carried alongside it, because
    `client_ceilings.POINT_RE` is what parsed the name in the first place and a
    second copy of the convention is a second thing to keep in step.
    """
    return (f"lat-{key.engine}-b{key.batch}-c{key.concurrency}"
            f"-w{key.workers}-r{key.rep}-s0.jsonl")


def header_of(path: str | os.PathLike) -> dict[str, Any]:
    """Just the header record.

    Not `runmeta.read_jsonl`: that returns the data records too, and at
    ScyllaDB's one document per operation those are the whole 400,000-line
    artifact.
    """
    try:
        with open(path, "r", encoding="utf-8") as stream:
            first = stream.readline()
    except OSError:
        return {}
    try:
        return json.loads(first) if first.strip() else {}
    except json.JSONDecodeError:
        return {}


def range_pct(values: Sequence[float]) -> float:
    """Peak-to-trough as a percentage of the peak. 0.0 for fewer than two."""
    usable = [value for value in values if value]
    if len(usable) < 2 or not max(usable):
        return 0.0
    return 100.0 * (max(usable) - min(usable)) / max(usable)


@dataclass(frozen=True)
class Rung:
    """One concurrency, and every repetition measured at it."""

    concurrency: int
    points: tuple[client_ceilings.PointResult, ...]

    def values(self, metric: Metric) -> list[float]:
        return [value for value in (metric(point) for point in self.points)
                if value is not None]

    def median(self, metric: Metric) -> float | None:
        values = self.values(metric)
        return statistics.median(values) if values else None

    def spread(self, metric: Metric) -> dict[str, float]:
        values = self.values(metric)
        return {**plotlib.spread(values), "range_pct": round(range_pct(values), 3)}

    def rep_of(self, rep: int, metric: Metric) -> float | None:
        for point in self.points:
            if point.key.rep == rep:
                return metric(point)
        return None

    def median_point(self, metric: Metric
                     ) -> client_ceilings.PointResult | None:
        """The repetition actually drawn, chosen the way plotlib chooses: the
        lower-middle value, so a real run is drawn rather than an average of
        runs that never happened."""
        pairs = [(metric(point), point) for point in self.points
                 if metric(point) is not None]
        if not pairs:
            return None
        values = [value for value, _ in pairs]
        return pairs[plotlib.median_index(values)][1]

    @property
    def cores_available(self) -> int:
        return max((point.cost.cores_available for point in self.points
                    if point.cost), default=0)


@dataclass(frozen=True)
class Arm:
    """One engine at one worker count: one line on every chart here."""

    engine: str
    workers: int
    batch: int
    rungs: tuple[Rung, ...]
    data_dir: str

    @property
    def name(self) -> str:
        return f"{self.engine} N={self.workers}"

    @property
    def style(self) -> dict[str, Any]:
        return {"color": ENGINE_COLORS.get(self.engine, ENGINE_FALLBACK),
                **WORKER_STYLES.get(self.workers, WORKER_FALLBACK)}

    @property
    def reps(self) -> list[int]:
        return sorted({point.key.rep for rung in self.rungs
                       for point in rung.points})

    @property
    def cores_available(self) -> int:
        return max((rung.cores_available for rung in self.rungs), default=0)

    def shard_zero_files(self) -> list[str]:
        """One artifact per repetition per rung — the provenance view.

        Shard 0 only, on purpose: `--max-docs` is a per-process share, so at a
        budget the worker count does not divide the shards legitimately differ
        by one document, and `plotlib.assert_one_measurement` compares
        `max_docs` across the runs it is given. Handed every shard it would
        refuse a correct point.
        """
        return [os.path.join(self.data_dir, shard_zero_name(point.key))
                for rung in self.rungs for point in rung.points]

    def drawable(self, metric: Metric) -> list[Rung]:
        return [rung for rung in self.rungs if rung.median(metric) is not None]

    def best_rung(self, metric: Metric) -> Rung | None:
        """The rung with the highest median — the arm's ceiling, as measured."""
        drawable = self.drawable(metric)
        if not drawable:
            return None
        return max(drawable, key=lambda rung: rung.median(metric) or 0.0)

    def is_lower_bound(self, metric: Metric) -> bool:
        """True when the best rung is the TOP rung.

        A ceiling found at the end of the ladder has not been shown to be one:
        the rung above might have been higher, and nothing here says otherwise.
        `client_ceilings.ceiling_rungs` draws the same distinction for the same
        reason, and the chart draws these markers hollow.
        """
        drawable = self.drawable(metric)
        return (len(drawable) > 1
                and self.best_rung(metric) is drawable[-1])


def _rungs(points: Sequence[client_ceilings.PointResult]) -> tuple[Rung, ...]:
    by_concurrency: dict[int, list[client_ceilings.PointResult]] = {}
    for point in points:
        by_concurrency.setdefault(point.key.concurrency, []).append(point)
    return tuple(
        Rung(concurrency, tuple(sorted(group, key=lambda p: p.key.rep)))
        for concurrency, group in sorted(by_concurrency.items()))


def load_arms(data_dir: str | os.PathLike) -> list[Arm]:
    """Every (engine, worker count, batch) the directory holds, in roster order.

    Engine-then-workers rather than alphabetically, so the legend, the summary
    and the campaign's own line order agree.
    """
    directory = Path(data_dir)
    grouped: dict[tuple[str, int, int], list[client_ceilings.PointResult]] = {}
    for point in client_ceilings.collect(directory):
        key = (point.key.engine, point.key.workers, point.key.batch)
        grouped.setdefault(key, []).append(point)
    arms = [Arm(engine=engine, workers=workers, batch=batch,
                rungs=_rungs(points), data_dir=str(directory))
            for (engine, workers, batch), points in grouped.items()]
    order = list(ENGINE_COLORS)
    return sorted(arms, key=lambda arm: (
        order.index(arm.engine) if arm.engine in order else len(order),
        arm.workers))


def config_series(arm: Arm, metric: Metric) -> plotlib.ConfigSeries:
    """The provenance view of one arm, so the footer and the sidecar come from
    `plotlib` rather than from here.

    The drawn run is the best rung's median repetition, because the best rung is
    what the arm is quoted at. Corpus agreement is checked across every rung,
    because one smoke repetition anywhere on the ladder flattens the curve
    wherever it lands.
    """
    every = [plotlib.Run(path=path, header=header_of(path), records=[])
             for path in arm.shard_zero_files()]
    plotlib.assert_one_measurement(arm.name, every)
    rung = arm.best_rung(metric)
    points = rung.points if rung else ()
    runs = [plotlib.Run(
        path=os.path.join(arm.data_dir, shard_zero_name(point.key)),
        header=header_of(os.path.join(arm.data_dir,
                                      shard_zero_name(point.key))),
        records=[]) for point in points]
    metrics = [value for value in (metric(point) for point in points)
               if value is not None]
    if not runs or len(metrics) != len(runs):
        # Provenance without a metric: the footer still has to name the engine
        # version, cache state and corpus, and index 0 is a real run.
        return plotlib.ConfigSeries(name=arm.name, runs=every,
                                    metrics=[0.0] * len(every), chosen_index=0)
    return plotlib.ConfigSeries(name=arm.name, runs=runs, metrics=metrics,
                                chosen_index=plotlib.median_index(metrics))


def flatness(arm: Arm, metric: Metric) -> dict[str, float]:
    """How much the arm's median moved across the ladder, against how much one
    rung's repetitions moved.

    The number this chart exists to establish or refute. P0 measured the
    OpenSearch client flat in concurrency to within +-3% at N=1, and a ladder
    whose whole range sits inside its own repetition spread is not a curve — it
    is one value drawn five times, and saying so is the finding rather than a
    disappointment.
    """
    medians = [rung.median(metric) for rung in arm.drawable(metric)]
    medians = [value for value in medians if value]
    if len(medians) < 2:
        return {}
    reps = [rung.spread(metric).get("range_pct", 0.0)
            for rung in arm.drawable(metric)]
    return {"ladder_range_pct": round(range_pct(medians), 2),
            "worst_repetition_range_pct": round(max(reps) if reps else 0.0, 2)}


def flat_arms(arms: Sequence[Arm], metric: Metric) -> list[tuple[Arm, float]]:
    flat = []
    for arm in arms:
        figures = flatness(arm, metric)
        if figures and figures["ladder_range_pct"] <= \
                figures["worst_repetition_range_pct"]:
            flat.append((arm, figures["ladder_range_pct"]))
    return flat


def flatness_note(arms: Sequence[Arm], metric: Metric) -> str:
    """One footer sentence, worded from the data rather than from the plan."""
    flat = flat_arms(arms, metric)
    if not flat:
        return ""
    named = ", ".join(f"{arm.name} {pct:.1f}%" for arm, pct in flat)
    return (f"FLAT IN CONCURRENCY — the whole ladder range sits inside one "
            f"rung's repetition spread for: {named}. For those arms the x axis "
            f"carries no signal and the line is one value drawn repeatedly.")


def oversubscription_note(arms: Sequence[Arm]) -> str:
    """The N=8 caveat, stated only when such an arm is actually drawn."""
    cores = max((arm.cores_available for arm in arms), default=0)
    crowded = sorted({arm.workers for arm in arms
                      if cores and arm.workers >= cores})
    if not crowded:
        return ""
    counts = "/".join(str(count) for count in crowded)
    return (f"N={counts} is oversubscribed on a {cores}-core box: that many "
            f"loader processes plus the CPU probe do not fit, the measured "
            f"per-loader cost being ~1.0 core (OpenSearch) and ~1.1 "
            f"(ScyllaDB). Those lines measure this box's core count, not the "
            f"loader's process scalability, and cannot raise Phase 0's "
            f"N_max >= 4.")


def floor_note(arms: Sequence[Arm]) -> str:
    rungs = sorted({rung.concurrency for arm in arms for rung in arm.rungs})
    workers = max((arm.workers for arm in arms), default=0)
    if not rungs or not workers:
        return ""
    return (f"x is the run's TOTAL operations in flight, split across the N "
            f"loader processes, so the lowest per-process figure drawn is "
            f"{rungs[0]}/{workers} = {rungs[0] // workers}. Every rung is a "
            f"multiple of lcm(4,6,8)=24 so all three worker counts divide it "
            f"exactly; the region below that per-process figure is not on this "
            f"chart.")


SINK_NOTE = ("Sink: ftsbench.null_sink — accept and discard, TCP_QUICKACK set, "
             "two instances per protocol with a point's loaders spread across "
             "them, because one asyncio loop in one process would itself be "
             "the ceiling. This is the CLIENT's rate: no engine was running.")
HOLLOW_NOTE = ("A hollow marker is the arm's best rung AND its top rung, so "
               "that arm's ceiling was never bracketed — read it as a lower "
               "bound.")


def summary_lines(arms: Sequence[Arm], metric: Metric,
                  metric_name: str) -> list[str]:
    lines = []
    for arm in arms:
        best = arm.best_rung(metric)
        if best is None:
            continue
        qualifier = "  LOWER BOUND (best at the top rung)" \
            if arm.is_lower_bound(metric) else ""
        lines.append(f"{arm.name:<20} best c={best.concurrency:<5} "
                     f"{metric_name}={best.median(metric):>12,.2f}  "
                     f"N={len(best.points)}{qualifier}")
    return lines


def arm_sidecar(arm: Arm, metric: Metric) -> dict[str, Any]:
    """Every rung and every metric, so the JSON answers questions the PNG's one
    axis cannot — requests/s among them."""
    return {
        "engine": arm.engine,
        "workers": arm.workers,
        "batch_size": arm.batch,
        "cores_available": arm.cores_available,
        "repetitions": arm.reps,
        "is_lower_bound": arm.is_lower_bound(metric),
        "flatness": flatness(arm, metric),
        "rungs": [
            {
                "concurrency": rung.concurrency,
                "repetitions": len(rung.points),
                "per_process_concurrency": rung.concurrency // arm.workers,
                **{name: [accessor(point) for point in rung.points]
                   for name, accessor in METRICS.items()},
                "spread": rung.spread(metric),
            }
            for rung in arm.rungs
        ],
    }
