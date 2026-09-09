"""Turn Phase 0's null-sink runs into the two constants the campaign is blocked on.

Reads a `tools/client_calibration.sh` output directory and writes the
`--ceilings` JSON `ftsbench.verify_generator` consumes:

    {"engine": "opensearch", "measured_on": "fts-harness i8g.2xlarge",
     "ops_per_s": {"16": 1863.0, ...}, "loader_core_bound_at": 0.70}

Two rules, both there because the gate exists to stop guesses:

- **A batch level with no run does not get a number.** `ops_per_s` carries only
  the levels that were measured. The gate refuses an unmeasured level, and a
  level interpolated here would defeat that refusal one file earlier.
- **`loader_core_bound_at` is measured or absent.** `ftsbench.verify_generator`
  applies that one fraction to two budgets — the box's cores, and one core for
  one thread — so it is a fraction of a CPU. It comes from the single-process
  ceiling runs, where the null sink makes the client the constraint by
  construction, so the level a busiest thread reaches there is the level a bound
  loader sits at. The threshold sits `SATURATION_MARGIN` below that, for the
  same reason the plan's 0.70 sat below a measured ~0.80 — a threshold set AT
  saturation never fires — but with the anchor replaced by the current client's
  own figure.

The worker ladder answers the OTHER question, `N_max`, and is reported
separately. Conflating the two is a live hazard: a first pass here derived
`loader_core_bound_at` from the ladder's box utilisation and handed the gate
0.235, which is a plausible box fraction and an absurd per-thread bound.

`N_max` itself is only computed when the caller states the ENGINE's ceiling,
which this session does not measure. The ladder's knee, the per-process rate and
the stated headroom are all recorded; the arithmetic between them is not
performed on an assumed engine rate.

The ceiling per level is the MAX over the concurrency rungs of the MEDIAN over
repetitions. Max over rungs because a ceiling is the best the client managed;
median within a rung because one cold repetition should not raise it.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import runmeta

POINT_RE = re.compile(
    r"^lat-(?P<engine>[a-z]+)-b(?P<batch>\d+)-c(?P<concurrency>\d+)"
    r"-w(?P<workers>\d+)-r(?P<rep>\d+)-s(?P<shard>\d+)\.jsonl$")
# The first probe tick has no rate at all (it is a difference), and the second
# still covers the client's connect and prepare. Both would drag a steady-state
# CPU figure down.
WARM_IN_TICKS = 2
SCALING_THRESHOLD = 1.1
ONE_CORE = 1.0
# How far below measured saturation the threshold sits. A bound set AT the level
# a pinned thread reaches would never fire; the plan's 0.70 sat below a measured
# ~0.80 for the same reason, and this keeps the shape of that choice while
# replacing its anchor with a figure from the current client.
SATURATION_MARGIN = 0.85
# BUILD-RATE-MATRIX-PLAN.md's own figure for what N_max must buy.
DEFAULT_HEADROOM = 3.0


@dataclass(frozen=True)
class PointKey:
    engine: str
    batch: int
    concurrency: int
    workers: int
    rep: int

    def probe_name(self) -> str:
        return (f"gen-{self.engine}-b{self.batch}-c{self.concurrency}"
                f"-w{self.workers}-r{self.rep}.jsonl")

    def as_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "batch_size": self.batch,
                "concurrency": self.concurrency, "workers": self.workers,
                "rep": self.rep}


@dataclass(frozen=True)
class ShardResult:
    ok_ops: int
    ok_docs: int
    errors: int
    wall_s: float


@dataclass(frozen=True)
class LoaderCost:
    """What the harness box spent, as the generator probe saw it."""

    cores_available: int
    process_cores_p50: float
    process_cores_max: float
    busiest_thread_cores_max: float
    loaders_cores_p50: float
    box_cores_p50: float
    box_cores_max: float

    @property
    def box_fraction_p50(self) -> float:
        if not self.cores_available:
            return 0.0
        return self.box_cores_p50 / self.cores_available

    def as_dict(self) -> dict[str, Any]:
        return {
            "cores_available": self.cores_available,
            "loader_process_cores_p50": round(self.process_cores_p50, 3),
            "loader_process_cores_max": round(self.process_cores_max, 3),
            "busiest_thread_cores_max": round(self.busiest_thread_cores_max, 3),
            "loaders_cores_p50": round(self.loaders_cores_p50, 3),
            "box_cores_p50": round(self.box_cores_p50, 3),
            "box_cores_max": round(self.box_cores_max, 3),
            "box_fraction_p50": round(self.box_fraction_p50, 4),
        }


@dataclass(frozen=True)
class PointResult:
    key: PointKey
    shards: int
    ok_ops: int
    ok_docs: int
    errors: int
    wall_s: float
    cost: LoaderCost | None

    @property
    def docs_per_s(self) -> float:
        return self.ok_docs / self.wall_s if self.wall_s > 0 else 0.0

    @property
    def ops_per_s(self) -> float:
        return self.ok_ops / self.wall_s if self.wall_s > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.key.as_dict(),
            "shards": self.shards,
            "ok_ops": self.ok_ops,
            "ok_docs": self.ok_docs,
            "errors": self.errors,
            "wall_s": round(self.wall_s, 3),
            "docs_per_s": round(self.docs_per_s, 1),
            "ops_per_s": round(self.ops_per_s, 1),
            "generator": self.cost.as_dict() if self.cost else None,
        }


def read_shard(path: Path) -> ShardResult:
    """One loader process's latency log, reduced to what a rate needs.

    The wall is the last operation's end, which is relative to the driver's own
    origin — stamped before the first document is read and after the client has
    connected. Dividing by a shell-measured wall instead would fold the client's
    start-up into the rate and understate every ceiling.
    """
    _, records = runmeta.read_jsonl(path)
    operations = [record for record in records
                  if record.get("record") == "latency_op"]
    successes = [record for record in operations if record.get("ok")]
    return ShardResult(
        ok_ops=len(successes),
        ok_docs=sum(record.get("n_docs") or 0 for record in successes),
        errors=len(operations) - len(successes),
        wall_s=max((record.get("t_end_s") or 0.0 for record in operations),
                   default=0.0))


def _values(records: list[dict[str, Any]], field: str) -> list[float]:
    return [float(record[field]) for record in records
            if record.get(field) is not None]


def _by_tick(records: list[dict[str, Any]],
             name: str) -> dict[int, list[dict[str, Any]]]:
    ticks: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("record") == name and (record.get("i") or 0) >= WARM_IN_TICKS:
            ticks[record["i"]].append(record)
    return ticks


def _tick_sums(ticks: dict[int, list[dict[str, Any]]]) -> list[float]:
    """Loader CPU summed within a tick, so N processes read as one figure."""
    sums = []
    for records in ticks.values():
        cores = _values(records, "cpu_cores_used")
        if cores:
            sums.append(sum(cores))
    return sums


def loader_cost(path: Path) -> LoaderCost | None:
    if not path.exists():
        return None
    _, records = runmeta.read_jsonl(path)
    loaders = [record for tick in _by_tick(records, "generator_sample").values()
               for record in tick]
    box = [record for tick in _by_tick(records, "generator_box_sample").values()
           for record in tick]
    process_cores = _values(loaders, "cpu_cores_used")
    box_cores = _values(box, "cpu_cores_used")
    cores = max((int(record.get("cores_available") or 0) for record in box),
                default=0)
    # No core count means no box fraction, and a fraction of zero cores would
    # emit 0.0 as `loader_core_bound_at` — a number, from nothing.
    if not process_cores or not box_cores or not cores:
        return None
    aggregate = _tick_sums(_by_tick(records, "generator_sample"))
    return LoaderCost(
        cores_available=cores,
        process_cores_p50=statistics.median(process_cores),
        process_cores_max=max(process_cores),
        busiest_thread_cores_max=max(
            _values(loaders, "busiest_thread_cores") or [0.0]),
        loaders_cores_p50=statistics.median(aggregate or process_cores),
        box_cores_p50=statistics.median(box_cores),
        box_cores_max=max(box_cores))


def discover(data_dir: Path) -> dict[PointKey, list[Path]]:
    found: dict[PointKey, list[Path]] = defaultdict(list)
    for path in sorted(data_dir.glob("lat-*.jsonl")):
        match = POINT_RE.match(path.name)
        if match is None:
            continue
        found[PointKey(match["engine"], int(match["batch"]),
                       int(match["concurrency"]), int(match["workers"]),
                       int(match["rep"]))].append(path)
    return found


def point_result(key: PointKey, paths: list[Path],
                 data_dir: Path) -> PointResult:
    """One point's totals across its worker processes.

    The wall is the SLOWEST process's, not the sum and not the mean: the workers
    are launched together and the run is over when the last one finishes, so any
    other choice reports a rate the point never sustained.
    """
    shards = [read_shard(path) for path in paths]
    return PointResult(
        key=key, shards=len(shards),
        ok_ops=sum(shard.ok_ops for shard in shards),
        ok_docs=sum(shard.ok_docs for shard in shards),
        errors=sum(shard.errors for shard in shards),
        wall_s=max((shard.wall_s for shard in shards), default=0.0),
        cost=loader_cost(data_dir / key.probe_name()))


def collect(data_dir: Path) -> list[PointResult]:
    ordered = sorted(discover(data_dir).items(),
                     key=lambda item: dataclasses.astuple(item[0]))
    return [point_result(key, paths, data_dir) for key, paths in ordered]


def single_process(points: list[PointResult], engine: str) -> list[PointResult]:
    return [point for point in points
            if point.key.engine == engine and point.key.workers == 1]


def _median_per_rung(points: list[PointResult],
                     metric: str) -> dict[int, dict[int, float]]:
    rungs: dict[int, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    for point in points:
        rungs[point.key.batch][point.key.concurrency].append(
            getattr(point, metric))
    return {batch: {rung: statistics.median(values)
                    for rung, values in by_rung.items()}
            for batch, by_rung in rungs.items()}


def ops_ceilings(points: list[PointResult]) -> dict[str, float]:
    return {str(batch): round(max(by_rung.values()), 1)
            for batch, by_rung in _median_per_rung(points, "ops_per_s").items()}


def docs_ceilings(points: list[PointResult]) -> dict[str, float]:
    return {str(batch): round(max(by_rung.values()), 1)
            for batch, by_rung in _median_per_rung(points, "docs_per_s").items()}


def ceiling_rungs(points: list[PointResult]) -> dict[str, int]:
    """Which concurrency rung the ceiling came from — a ceiling found at the
    top rung has not been shown to be one."""
    rungs = _median_per_rung(points, "ops_per_s")
    return {str(batch): max(by_rung, key=lambda rung: by_rung[rung])
            for batch, by_rung in rungs.items()}


def worker_ladder(points: list[PointResult],
                  engine: str) -> list[PointResult]:
    """The points that vary only the worker count.

    Found in the data rather than named by a flag: a ladder is a (batch,
    concurrency) cell holding more than one worker count, and the cell with the
    most of them is the ladder. Taking every point of the engine instead would
    fold the whole batch sweep into the N=1 rung and compare a batch-16 rate
    against a batch-512 one.
    """
    cells: dict[tuple[int, int], list[PointResult]] = defaultdict(list)
    for point in points:
        if point.key.engine == engine:
            cells[(point.key.batch, point.key.concurrency)].append(point)
    ladder = max(cells.values(),
                 key=lambda group: len({point.key.workers
                                        for point in group}),
                 default=[])
    return sorted(ladder, key=lambda point: point.key.workers)


def _aggregate_per_worker_count(ladder: list[PointResult]) -> dict[int, float]:
    rates: dict[int, list[float]] = defaultdict(list)
    for point in ladder:
        rates[point.key.workers].append(point.docs_per_s)
    return {workers: statistics.median(values)
            for workers, values in sorted(rates.items())}


@dataclass(frozen=True)
class CoreBound:
    """`loader_core_bound_at`, and the saturation it was derived from.

    `ftsbench.verify_generator` applies this one fraction to two budgets — the
    box's cores, and one core for one thread — so it is a fraction of a CPU, not
    a fraction of the box. The measurement is what a saturated loader thread
    actually reaches against the null sink, where the client is bound by
    construction; the threshold sits a stated margin below it, because a
    threshold set AT the saturation level would never fire.
    """

    fraction: float
    saturated_at: float
    margin: float
    points: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "loader_core_bound_at": round(self.fraction, 3),
            "saturated_thread_cores": round(self.saturated_at, 3),
            "margin_below_saturation": self.margin,
            "ceiling_points_measured": self.points,
            "basis": ("median busiest_thread_cores of the single-process "
                      "ceiling points, where the null sink makes the client "
                      f"bound by construction, times {SATURATION_MARGIN}"),
        }


@dataclass(frozen=True)
class WorkerCeiling:
    """Where another loader process stopped buying throughput."""

    workers: int
    is_lower_bound: bool
    rates: dict[int, float]
    box_fraction: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "is_lower_bound": self.is_lower_bound,
            "aggregate_docs_per_s_by_workers": {
                str(workers): round(rate, 1)
                for workers, rate in self.rates.items()},
            "box_fraction_at_that_count": (None if self.box_fraction is None
                                           else round(self.box_fraction, 4)),
            "basis": ("the worker count whose aggregate failed to beat the "
                      f"previous one by {SCALING_THRESHOLD}x"),
        }


@dataclass(frozen=True)
class CoreBoundOutcome:
    """The bound, or the reason there is none — never a substitute for it."""

    bound: CoreBound | None
    reason: str


NO_LADDER = ("the worker ladder holds fewer than two worker counts, so "
             "nothing here shows where adding a process stops helping")
NO_TICKS = (f"no single-process ceiling point produced usable generator-probe "
            f"ticks; a point has to outlast the first {WARM_IN_TICKS} probe "
            f"intervals before its CPU figure means anything")


def _box_fraction(ladder: list[PointResult], workers: int) -> float | None:
    fractions = [point.cost.box_fraction_p50 for point in ladder
                 if point.key.workers == workers and point.cost]
    return statistics.median(fractions) if fractions else None


def _knee_workers(rates: dict[int, float]) -> int:
    counts = list(rates)
    for previous, current in zip(counts, counts[1:]):
        if rates[current] < SCALING_THRESHOLD * rates[previous]:
            return current
    return counts[-1]


def _is_lower_bound(rates: dict[int, float], workers: int) -> bool:
    """The top of a ladder that was still scaling is a floor, not a bound."""
    counts = list(rates)
    return (workers == counts[-1]
            and rates[counts[-1]] >= SCALING_THRESHOLD * rates[counts[-2]])


def worker_ceiling(ladder: list[PointResult]) -> WorkerCeiling | None:
    """`N_max`'s evidence: the worker count at which the ladder flattened.

    `None` when the ladder holds one worker count. It does not set
    `loader_core_bound_at` — that is a per-core saturation figure and this is a
    process count, and conflating the two put a box-utilisation fraction of
    0.235 where the gate wanted ~0.85 of one core.
    """
    rates = _aggregate_per_worker_count(ladder)
    if len(rates) < 2:
        return None
    workers = _knee_workers(rates)
    return WorkerCeiling(workers, _is_lower_bound(rates, workers), rates,
                         _box_fraction(ladder, workers))


def core_bound(ceiling_points: list[PointResult]) -> CoreBoundOutcome:
    """The per-core fraction at which a loader thread counts as pinned.

    Taken from the single-process ceiling runs, not from the worker ladder: at
    the null sink one loader process IS the constraint, so the level its busiest
    thread reaches there is the level a bound loader sits at — measured on the
    async client rather than inherited from the threaded one the plan's 0.70 was
    anchored to.
    """
    peaks = [point.cost.busiest_thread_cores_max for point in ceiling_points
             if point.cost and point.cost.busiest_thread_cores_max > 0]
    if not peaks:
        return CoreBoundOutcome(None, NO_TICKS)
    # A thread cannot exceed a core; sampling two counters a tick apart can say
    # it did, and that arithmetic must not raise the threshold above 1.0.
    saturated = min(statistics.median(peaks), ONE_CORE)
    return CoreBoundOutcome(
        CoreBound(SATURATION_MARGIN * saturated, saturated, SATURATION_MARGIN,
                  len(peaks)), "")


def implied_worker_count(docs_per_s: float, engine_ceiling: float,
                         headroom: float) -> int:
    """Processes needed to offer `headroom` x an engine ceiling.

    Only computed when the caller states the engine ceiling: `N_max` is a
    function of a number this session does not measure, and picking one here
    would bury an assumed engine rate inside a client constant.
    """
    if docs_per_s <= 0:
        return 0
    return math.ceil(engine_ceiling * headroom / docs_per_s)


def ceilings_document(engine: str, measured_on: str, ops_per_s: dict[str, float],
                      bound: CoreBound | None) -> dict[str, Any]:
    """Exactly the four keys the gate reads, and nothing else.

    The provenance goes in a separate file on purpose: this one is a gate input,
    and a gate input that also carried the analysis would invite someone to edit
    the analysis and change the gate.
    """
    document: dict[str, Any] = {
        "engine": engine,
        "measured_on": measured_on,
        "ops_per_s": ops_per_s,
    }
    if bound is not None:
        document["loader_core_bound_at"] = round(bound.fraction, 3)
    return document


@dataclass(frozen=True)
class Derived:
    """Everything the two constants were derived from, in one place."""

    core_bound: CoreBoundOutcome
    workers: WorkerCeiling | None
    worker_count_note: dict[str, Any]


def worker_count_note(ceilings: dict[str, float], engine_ceiling: float | None,
                      headroom: float) -> dict[str, Any]:
    """`N_max` from headroom, only when the engine ceiling was stated."""
    if not engine_ceiling or not ceilings:
        return {"n_max": None,
                "not_computed_because": (
                    "N_max is a function of the ENGINE's ceiling, which this "
                    "session does not measure; pass --engine-ceiling-docs-per-s "
                    "to have it derived, or take it from the worker ladder")}
    best = max(ceilings.values())
    return {"n_max": implied_worker_count(best, engine_ceiling, headroom),
            "per_process_docs_per_s": round(best, 1),
            "engine_ceiling_docs_per_s": engine_ceiling,
            "headroom": headroom}


def derive(points: list[PointResult], engine: str,
           engine_ceiling: float | None, headroom: float) -> Derived:
    single = single_process(points, engine)
    return Derived(core_bound=core_bound(single),
                   workers=worker_ceiling(worker_ladder(points, engine)),
                   worker_count_note=worker_count_note(
                       docs_ceilings(single), engine_ceiling, headroom))


def provenance_document(engine: str, measured_on: str,
                        points: list[PointResult],
                        derived: Derived) -> dict[str, Any]:
    single = single_process(points, engine)
    outcome = derived.core_bound
    return {
        **runmeta.header(producer="client_ceilings", engine=engine,
                         engine_version="n/a — the client is what was measured",
                         label="Phase 0 client calibration", corpus="synthetic",
                         measured_on=measured_on),
        "sink": "ftsbench.null_sink — accept and discard, nothing stored",
        "ops_per_s_ceiling": ops_ceilings(single),
        "docs_per_s_ceiling": docs_ceilings(single),
        "ceiling_found_at_concurrency": ceiling_rungs(single),
        "core_bound": (outcome.bound.as_dict() if outcome.bound
                       else {"loader_core_bound_at": None,
                             "not_measured_because": outcome.reason}),
        "worker_ceiling": (derived.workers.as_dict() if derived.workers
                           else {"workers": None,
                                 "not_measured_because": NO_LADDER}),
        "worker_count": derived.worker_count_note,
        "points_without_generator_data": sum(1 for point in points
                                             if point.cost is None),
        "points": [point.as_dict() for point in points],
    }


def write_json(path: str, document: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True,
                        help="a tools/client_calibration.sh output directory")
    parser.add_argument("--engine", required=True,
                        choices=("opensearch", "scylladb"))
    parser.add_argument("--measured-on", required=True,
                        help="the box the LOADERS ran on; per-process "
                             "throughput is a function of its per-core speed, "
                             "so a figure without this is not transferable")
    parser.add_argument("--out", required=True, help="the --ceilings JSON")
    parser.add_argument("--provenance-out", default=None,
                        help="every point, and how the constants were derived "
                             "from them")
    parser.add_argument("--engine-ceiling-docs-per-s", type=float,
                        default=None,
                        help="the engine rate N_max must be able to outrun; "
                             "no default, because it is not measured here")
    parser.add_argument("--headroom", type=float, default=DEFAULT_HEADROOM,
                        help="how far the client must outrun the engine for "
                             "N_max")
    parser.add_argument("--quiet", action="store_true",
                        help="write the files without narrating them, for a "
                             "caller that only wants one number back out")
    return parser.parse_args(argv)


def report_ceilings(engine: str, ceilings: dict[str, Any]) -> None:
    for batch, rate in sorted(ceilings["ops_per_s"].items(),
                              key=lambda item: int(item[0])):
        print(f"{engine} batch {batch:>4}: {rate:9.1f} operations/s per process")


def report_bound(outcome: CoreBoundOutcome) -> None:
    if outcome.bound is None:
        print(f"loader_core_bound_at: NOT MEASURED — {outcome.reason}; the "
              f"gate will refuse rather than default", file=sys.stderr)
        return
    bound = outcome.bound
    print(f"loader_core_bound_at: {bound.fraction:.3f} of a core "
          f"({bound.margin} x a measured {bound.saturated_at:.3f} saturated "
          f"thread over {bound.points} ceiling point(s))")


def report_workers(workers: WorkerCeiling | None,
                   note: dict[str, Any]) -> None:
    if workers is None:
        print(f"worker ceiling: NOT MEASURED — {NO_LADDER}", file=sys.stderr)
    else:
        qualifier = " (LOWER BOUND — the ladder never stopped scaling)" \
            if workers.is_lower_bound else ""
        rates = ", ".join(f"N={count}: {rate:.0f} docs/s"
                          for count, rate in workers.rates.items())
        print(f"worker ceiling: {workers.workers}{qualifier} — {rates}")
    if note.get("n_max"):
        print(f"N_max for {note['headroom']}x "
              f"{note['engine_ceiling_docs_per_s']:.0f} docs/s: "
              f"{note['n_max']} processes")


def report(engine: str, ceilings: dict[str, Any], derived: Derived,
           blind_points: int) -> None:
    report_ceilings(engine, ceilings)
    if blind_points:
        print(f"{blind_points} point(s) produced no usable generator-probe "
              f"ticks — too short to carry a CPU figure", file=sys.stderr)
    report_bound(derived.core_bound)
    report_workers(derived.workers, derived.worker_count_note)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir)
    points = collect(data_dir)
    if not points:
        print(f"no calibration points in {data_dir} (expected "
              f"lat-<engine>-b<n>-c<n>-w<n>-r<n>-s<n>.jsonl)", file=sys.stderr)
        return 2
    derived = derive(points, args.engine, args.engine_ceiling_docs_per_s,
                     args.headroom)
    ceilings = ceilings_document(args.engine, args.measured_on,
                                 ops_ceilings(single_process(points,
                                                             args.engine)),
                                 derived.core_bound.bound)
    write_json(args.out, ceilings)
    if args.provenance_out:
        write_json(args.provenance_out,
                   provenance_document(args.engine, args.measured_on, points,
                                       derived))
    if not args.quiet:
        report(args.engine, ceilings, derived,
               sum(1 for point in points if point.cost is None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
