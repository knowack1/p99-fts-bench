"""Open-loop query load generator — the sample behind C5, C6 and C7.

`query_bench` sends the next query only after the previous one returns, so its
"rate" is whatever the engine allowed and its percentiles are service times. That
is fine for a smoke test and wrong for a latency SLA. This module offers a rate
the engine does not get to negotiate: `pacer` dispatches on a fixed schedule into
a bounded queue drained by a worker pool, and every record carries `latency_ms`
measured from the intended start.

Four decisions that determine whether the resulting percentiles mean anything:

- **A worker pool, not a thread.** At a fixed offered rate a single dispatcher
  serialises: it cannot have more than one request in flight, so the highest rate
  it can offer is 1/service_time and every rate above that silently becomes that
  one. `--concurrency` is the number of requests allowed in flight, and it is
  recorded in the header because it bounds every rate in the artifact.
- **The queue is bounded.** An unbounded backlog on a host already several GB
  into swap trades a measurement artifact for an OOM kill. When the queue fills
  the pacer blocks, the shortfall appears as `achieved_qps < offered_qps`, and
  `sweep` labels the point generator-saturated. A visible failure beats a silent
  one.
- **One collector thread writes the JSONL.** A flush per record inside a worker
  would put the harness's own file I/O on the measured path and report it as
  engine latency.
- **Queries are sampled uniformly from a seeded RNG, not cycled.** A round-robin
  over 20 queries at a fixed rate aliases with any periodic engine behaviour —
  segment refresh, cache eviction, compaction — and would write that period into
  the tail. The seed goes in the header, so the mix is reproducible without
  imposing a period of its own.

`--calibrate` measures what this generator can offer *on this machine*, which on
a laptop shared with the engine is the number that decides whether a C7 knee is a
finding or an artifact. See `calibrate` for what it can and cannot rule out.

Usage:
  python3 -m ftsbench.load_gen --engine opensearch --queries data/queries.json \\
      --calibrate --concurrency 16
  python3 -m ftsbench.load_gen --engine opensearch --queries data/queries.json \\
      --rate 400 --duration 60 --class rare_term --latency-log data/c5-opensearch-1.jsonl
"""
import argparse
import contextlib
import dataclasses
import itertools
import json
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, TextIO

from . import latency_log, pacer, runmeta
from .engines import DEFAULT_LIMIT, add_connection_args, build_engine
from .stats import percentile, summarize_or_empty

QUERY_CLASSES = ("rare_term", "common_term", "phrase",
                 "bool_and", "bool_not", "bool_mixed")
SEARCH_OP = "search"
CALIBRATION_CLASS = "calibration"
# Not a word in any language the corpus contains, so both parsers resolve it to
# an empty postings list and neither engine scores or fetches anything.
UNMATCHABLE_QUERY = "zqxjvkbnwmfltdgh"
DEFAULT_CONCURRENCY = 16
DEFAULT_DURATION_S = 60.0
DEFAULT_WARMUP_S = 10.0
DEFAULT_CALIBRATE_DURATION_S = 10.0
DEFAULT_SEED = 99
QUEUE_DEPTH_PER_WORKER = 4
MAX_CALIBRATION_OPS = 100_000_000
PROBE_FAILED_HITS = -1
SHUTDOWN = object()

SATURATION_OFFERED_FRACTION = 0.5
SATURATION_ACHIEVED_FRACTION = 0.95
SATURATION_QUEUE_FRACTION = 0.25


@dataclass(frozen=True)
class Query:
    """One query text plus the class and index it came from, so a `latency_op`
    record can be traced back to the entry in the generated query set."""
    query_class: str
    query_i: int
    text: str


@dataclass(frozen=True)
class GeneratorSettings:
    concurrency: int = DEFAULT_CONCURRENCY
    limit: int = DEFAULT_LIMIT
    seed: int = DEFAULT_SEED


@dataclass(frozen=True)
class Outcome:
    """When a worker actually started and finished one search, and what came
    back. Separate from the intended start, which only the pacer knows."""
    t_start_s: float
    t_end_s: float
    hits: int | None
    error: str | None


@dataclass(frozen=True)
class Calibration:
    generator_ceiling_qps: float
    dispatch_ceiling_qps: float
    concurrency: int
    duration_s: float
    calibration_hits: int


@dataclass
class RunTally:
    """The percentile sample for one window, kept as bare numbers rather than
    records: a 60 s window at a few thousand qps is hundreds of thousands of
    operations, and holding the dicts would cost more memory than this host has
    to spare."""
    latency_ms: list[float] = field(default_factory=list)
    service_ms: list[float] = field(default_factory=list)
    queue_ms: list[float] = field(default_factory=list)
    per_class_latency_ms: dict[str, list[float]] = field(default_factory=dict)
    errors: int = 0
    first_error: str | None = None

    @property
    def completed(self) -> int:
        """Every operation the generator got a verdict on. `achieved_qps` is
        derived from this and not from the successes alone, so an engine that
        fails fast is not also accused of starving the generator."""
        return len(self.latency_ms) + self.errors

    def add(self, record: dict[str, Any]) -> None:
        """A failed operation is counted and then left out of every percentile
        (SCHEMAS.md): including it would score an error's latency, and dropping
        it entirely would turn a failing engine into a fast one."""
        if not record["ok"]:
            self._add_error(record)
            return
        self.latency_ms.append(record["latency_ms"])
        self.service_ms.append(record["service_ms"])
        self.queue_ms.append(record["queue_ms"])
        self.per_class_latency_ms.setdefault(record["class"], []).append(
            record["latency_ms"])

    def _add_error(self, record: dict[str, Any]) -> None:
        self.errors += 1
        if self.first_error is None:
            self.first_error = record["error"]


class NoOpEngine:
    """Answers instantly. A ceiling taken against it measures the pacer, the
    queues and the worker pool with no engine and no socket in the path."""

    def search(self, query_text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
        return []


def read_query_set(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def select_classes(classes: dict[str, list[str]],
                   class_filter: str | None) -> dict[str, list[str]]:
    if class_filter is None:
        return {name: queries for name, queries in classes.items() if queries}
    if class_filter not in classes:
        raise ValueError(f"query set has no class {class_filter!r}")
    return {class_filter: classes[class_filter]}


def build_plan(query_set: dict[str, Any], class_filter: str | None) -> list[Query]:
    selected = select_classes(query_set["classes"], class_filter)
    plan = [Query(name, i, text)
            for name, queries in selected.items()
            for i, text in enumerate(queries)]
    if not plan:
        raise ValueError("no queries selected; check --class against the query set")
    return plan


def load_query_plan(path: str, class_filter: str | None = None) -> list[Query]:
    return build_plan(read_query_set(path), class_filter)


def calibration_plan() -> list[Query]:
    return [Query(CALIBRATION_CLASS, 0, UNMATCHABLE_QUERY)]


def run_operation(engine: Any, query: Query, limit: int) -> Outcome:
    """Broad except by design: a benchmark that dies on the first refused
    connection reports nothing, whereas an error rate is a finding."""
    t_start_s = time.perf_counter()
    try:
        hits: int | None = len(engine.search(query.text, limit))
        error = None
    except Exception as exc:
        hits, error = None, f"{type(exc).__name__}: {exc}"
    return Outcome(t_start_s, time.perf_counter(), hits, error)


def latency_op_record(op: pacer.Op, query: Query, outcome: Outcome,
                      origin_s: float) -> dict[str, Any]:
    """Delegates the field list to latency_log so the query harness and the
    ingest loaders cannot drift apart on the `latency_op` schema."""
    relative = latency_log.OpTiming(
        i=op.i, t_intended_s=op.t_intended_s,
        t_start_s=outcome.t_start_s, t_end_s=outcome.t_end_s,
    ).relative_to(origin_s)
    latencies = pacer.latencies_ms(relative.t_intended_s, relative.t_start_s,
                                   relative.t_end_s)
    return latency_log.build_latency_op(
        relative, latencies, SEARCH_OP,
        query_class=query.query_class, query_i=query.query_i,
        hits=outcome.hits, ok=outcome.error is None, error=outcome.error,
    )


def queue_capacity(concurrency: int) -> int:
    """Bounded so an overloaded run cannot become an OOM kill on a host already
    swapping. When it fills, the pacer blocks and the shortfall surfaces as
    achieved_qps below offered_qps instead of as silent memory growth."""
    return max(1, concurrency * QUEUE_DEPTH_PER_WORKER)


def worker_loop(engine: Any, work: queue.Queue, results: queue.Queue,
                limit: int, origin_s: float) -> None:
    while True:
        item = work.get()
        if item is SHUTDOWN:
            work.task_done()
            return
        op, query = item
        outcome = run_operation(engine, query, limit)
        results.put(latency_op_record(op, query, outcome, origin_s))
        work.task_done()


def start_workers(engine: Any, work: queue.Queue, results: queue.Queue,
                  settings: GeneratorSettings,
                  origin_s: float) -> list[threading.Thread]:
    workers = [
        threading.Thread(target=worker_loop, daemon=True,
                         args=(engine, work, results, settings.limit, origin_s))
        for _ in range(settings.concurrency)
    ]
    for worker in workers:
        worker.start()
    return workers


def stop_workers(workers: list[threading.Thread], work: queue.Queue) -> None:
    work.join()
    for _ in workers:
        work.put(SHUTDOWN)
    for worker in workers:
        worker.join()


def collect_results(results: queue.Queue, tally: RunTally,
                    sink: TextIO | None) -> None:
    """One consumer, so the per-record flush stays off the measured path: a write
    inside a worker would be timed as part of the engine's response."""
    while True:
        record = results.get()
        if record is SHUTDOWN:
            return
        tally.add(record)
        if sink is not None:
            runmeta.write_record(sink, record)


def start_collector(results: queue.Queue, tally: RunTally,
                    sink: TextIO | None) -> threading.Thread:
    collector = threading.Thread(target=collect_results, daemon=True,
                                 args=(results, tally, sink))
    collector.start()
    return collector


def stop_collector(collector: threading.Thread, results: queue.Queue) -> None:
    results.put(SHUTDOWN)
    collector.join()


def feed_work(work: queue.Queue, ops: Iterable[pacer.Op], plan: list[Query],
              rng: random.Random) -> None:
    for op in ops:
        work.put((op, rng.choice(plan)))


def drive_ops(engine: Any, plan: list[Query], ops: Iterable[pacer.Op],
              settings: GeneratorSettings, origin_s: float,
              sink: TextIO | None = None) -> RunTally:
    """Push an op stream through the worker pool. The stream decides the shape of
    the load: `pacer.paced_for_duration` for a measured window at a fixed offered
    rate, `saturating_ops` for a ceiling."""
    work: queue.Queue = queue.Queue(maxsize=queue_capacity(settings.concurrency))
    results: queue.Queue = queue.Queue()
    tally = RunTally()
    workers = start_workers(engine, work, results, settings, origin_s)
    collector = start_collector(results, tally, sink)
    feed_work(work, ops, plan, random.Random(settings.seed))
    stop_workers(workers, work)
    stop_collector(collector, results)
    return tally


def run_window(engine: Any, plan: list[Query], rate_per_s: float,
               duration_s: float, settings: GeneratorSettings,
               sink: TextIO | None = None) -> RunTally:
    """One fixed-rate window. Warmup is a separate call whose tally is thrown
    away, so the measured sample never has to be trimmed after the fact — a
    trimmed window is one where the reader has to trust the trim."""
    origin_s = time.perf_counter()
    ops = pacer.paced_for_duration(rate_per_s, duration_s, origin_s)
    return drive_ops(engine, plan, ops, settings, origin_s, sink)


def saturating_ops(duration_s: float) -> Iterator[pacer.Op]:
    """Closed-loop dispatch bounded by wall time. Correct for a ceiling, where
    the question is "how fast can this go" — and wrong for anything published as
    a latency, which is why it is confined to calibration."""
    deadline = time.perf_counter() + duration_s
    return itertools.takewhile(lambda op: time.perf_counter() < deadline,
                               pacer.unpaced(MAX_CALIBRATION_OPS))


def measure_ceiling(engine: Any, settings: GeneratorSettings,
                    duration_s: float) -> float:
    origin_s = time.perf_counter()
    tally = drive_ops(engine, calibration_plan(), saturating_ops(duration_s),
                      settings, origin_s)
    return tally.completed / (time.perf_counter() - origin_s)


def calibration_query_hits(engine: Any, limit: int) -> int:
    """A calibration query that matches documents makes the engine score and
    fetch them, so the ceiling would include work the cheapest path never does.
    PROBE_FAILED_HITS means the probe itself errored and the ceiling below is an
    error rate, not a query rate."""
    outcome = run_operation(engine, calibration_plan()[0], limit)
    return outcome.hits if outcome.hits is not None else PROBE_FAILED_HITS


def calibrate(engine: Any, settings: GeneratorSettings,
              duration_s: float) -> Calibration:
    """Measure how fast this generator can go, twice.

    `generator_ceiling_qps` is measured against the live engine using a query
    that matches nothing: everything a real query needs — client library, socket,
    engine dispatch, response parse — at the engine's cheapest possible work. It
    is the number `sweep` divides by, because it is the one on the same scale as
    the offered rates in the ladder.

    `dispatch_ceiling_qps` is measured against `NoOpEngine`: the pacer, queues
    and pool alone. Reported alongside so a reader can see which side binds. If
    the two are close, the harness is the limit; if the first is far lower, the
    request path is.

    What this cannot rule out. It is one number from one moment: taken before the
    ladder, on a machine whose cores run from 2.5 to 4.8 GHz, whose scheduler may
    place the pool differently next time, and which has other containers on it. A
    ceiling measured while the engine is otherwise idle overstates what the
    generator can offer while the engine is under load — the two compete for the
    same cores, so the real ceiling during a loaded rung is lower than this and
    unmeasurable without a second machine. It also cannot separate "the generator
    cannot offer more" from "the pool has too few workers for this service time":
    both cap the rate, and only the second is fixable with a flag. Treat it as an
    upper bound on offered rate, never as engine capacity.
    """
    hits = calibration_query_hits(engine, settings.limit)
    engine_qps = measure_ceiling(engine, settings, duration_s)
    dispatch_qps = measure_ceiling(NoOpEngine(), settings, duration_s)
    return Calibration(generator_ceiling_qps=round(engine_qps, 2),
                       dispatch_ceiling_qps=round(dispatch_qps, 2),
                       concurrency=settings.concurrency,
                       duration_s=duration_s, calibration_hits=hits)


def is_generator_saturated(offered_qps: float, achieved_qps: float,
                           queue_p99_ms: float, p99_ms: float,
                           generator_ceiling_qps: float,
                           completed_ops: int) -> bool:
    """Four independent ways a point stops being about the engine (SCHEMAS.md):
    the offered rate is within reach of the generator's own ceiling, the
    generator did not deliver the rate it promised, it held requests for a
    material fraction of the latency it is reporting, or nothing succeeded at
    all. Any one of them disqualifies the point from defining the C7 knee.

    The last one is not redundant. An engine shedding load returns its
    rejections fast, so achieved_qps still tracks the offered rate, and every
    percentile is computed over an empty sample and reported as zero — a rung
    where the engine refused every single request would otherwise pass all three
    of the other checks and win the SLA knee at p99 = 0 ms."""
    if completed_ops <= 0:
        return True
    return (offered_qps > SATURATION_OFFERED_FRACTION * generator_ceiling_qps
            or achieved_qps < SATURATION_ACHIEVED_FRACTION * offered_qps
            or queue_p99_ms > SATURATION_QUEUE_FRACTION * p99_ms)


def queue_p99_ms(tally: RunTally) -> float:
    return round(percentile(sorted(tally.queue_ms), 99), 3) if tally.queue_ms else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("opensearch", "scylladb"), required=True)
    parser.add_argument("--queries", required=True,
                        help="query set JSON from generate_queries")
    parser.add_argument("--rate", type=float,
                        help="offered queries/s; required unless --calibrate")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--warmup", type=float, default=DEFAULT_WARMUP_S,
                        help="unmeasured window at the same rate, 0 to skip")
    parser.add_argument("--class", dest="query_class", choices=QUERY_CLASSES,
                        help="restrict the mix to one class (C5 headline runs)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help="requests allowed in flight")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--latency-log", help="JSONL latency_op output path")
    parser.add_argument("--calibrate", action="store_true",
                        help="measure this generator's own ceiling and exit")
    parser.add_argument("--calibrate-duration", type=float,
                        default=DEFAULT_CALIBRATE_DURATION_S)
    parser.add_argument("--label", default="")
    parser.add_argument("--cache-state", default="unspecified")
    parser.add_argument("--engine-version", default="unknown")
    add_connection_args(parser)
    return require_rate_unless_calibrating(parser)


def require_rate_unless_calibrating(
        parser: argparse.ArgumentParser) -> argparse.Namespace:
    args = parser.parse_args()
    if args.rate is None and not args.calibrate:
        parser.error("--rate is required unless --calibrate is given")
    return args


def run_header(args: argparse.Namespace, query_set: dict[str, Any]) -> dict[str, Any]:
    return runmeta.header(
        producer="load_gen", engine=args.engine,
        engine_version=args.engine_version, label=args.label,
        cache_state=args.cache_state, corpus=query_set.get("corpus", ""),
        queries=args.queries, query_class=args.query_class or "all",
        offered_qps=args.rate, duration_s=args.duration, warmup_s=args.warmup,
        concurrency=args.concurrency, limit=args.limit, seed=args.seed)


@contextlib.contextmanager
def open_latency_log(path: str | None,
                     header: dict[str, Any]) -> Iterator[TextIO | None]:
    if path is None:
        yield None
        return
    with open(path, "w", encoding="utf-8") as handle:
        runmeta.write_record(handle, header)
        yield handle


def measured_run(engine: Any, plan: list[Query], settings: GeneratorSettings,
                 args: argparse.Namespace, sink: TextIO | None) -> RunTally:
    if args.warmup > 0:
        run_window(engine, plan, args.rate, args.warmup, settings)
    return run_window(engine, plan, args.rate, args.duration, settings, sink)


def print_rate_line(args: argparse.Namespace, tally: RunTally,
                    summary: dict[str, Any]) -> None:
    achieved_qps = tally.completed / args.duration
    print(f"offered={args.rate:g} qps  achieved={achieved_qps:.1f} qps  "
          f"count={summary['count']}  errors={tally.errors}", file=sys.stderr)


def print_percentile_line(summary: dict[str, Any]) -> None:
    print(f"latency_ms p50={summary['p50_ms']} p90={summary['p90_ms']} "
          f"p95={summary['p95_ms']} p99={summary['p99_ms']} "
          f"p99.9={summary['p999_ms']} p99.99={summary['p9999_ms']}",
          file=sys.stderr)


def print_omission_line(tally: RunTally, summary: dict[str, Any]) -> None:
    """queue_ms next to latency_ms on every summary, because that ratio is the
    only thing separating an engine measurement from a generator measurement."""
    print(f"queue_ms p99={queue_p99_ms(tally)} vs latency_ms "
          f"p99={summary['p99_ms']}", file=sys.stderr)


def warn_about_unsupported(summary: dict[str, Any]) -> None:
    unsupported = summary["unsupported_percentiles"]
    if unsupported:
        named = ", ".join(f"p{pct:g}" for pct in unsupported)
        print(f"  WARNING {summary['count']} samples do not support {named}; "
              f"do not plot them (stats.min_samples_for)", file=sys.stderr)


def warn_about_errors(tally: RunTally) -> None:
    if tally.errors:
        print(f"  WARNING {tally.errors} failed operations, first: "
              f"{tally.first_error}", file=sys.stderr)


def print_summary(args: argparse.Namespace, tally: RunTally) -> None:
    summary = summarize_or_empty(tally.latency_ms)
    print_rate_line(args, tally, summary)
    print_percentile_line(summary)
    print_omission_line(tally, summary)
    warn_about_unsupported(summary)
    warn_about_errors(tally)


def warn_about_calibration_query(calibration: Calibration) -> None:
    if calibration.calibration_hits > 0:
        print(f"  WARNING calibration query matched "
              f"{calibration.calibration_hits} documents, so the ceiling "
              f"includes scoring work", file=sys.stderr)
    elif calibration.calibration_hits == PROBE_FAILED_HITS:
        print("  WARNING calibration probe failed; the ceiling below is an "
              "error rate, not a query rate", file=sys.stderr)


def report_calibration(calibration: Calibration) -> int:
    """stdout carries the machine-readable line so TUNING.md can be filled from
    it; stderr carries the caveat, which is the part that must not get lost."""
    print(json.dumps(dataclasses.asdict(calibration)))
    print(f"generator_ceiling_qps={calibration.generator_ceiling_qps} "
          f"dispatch_ceiling_qps={calibration.dispatch_ceiling_qps} "
          f"concurrency={calibration.concurrency}", file=sys.stderr)
    warn_about_calibration_query(calibration)
    return 0


def main() -> int:
    args = parse_args()
    settings = GeneratorSettings(args.concurrency, args.limit, args.seed)
    engine = build_engine(args, pool_maxsize=args.concurrency)
    if args.calibrate:
        return report_calibration(
            calibrate(engine, settings, args.calibrate_duration))
    query_set = read_query_set(args.queries)
    plan = build_plan(query_set, args.query_class)
    with open_latency_log(args.latency_log, run_header(args, query_set)) as sink:
        tally = measured_run(engine, plan, settings, args, sink)
    print_summary(args, tally)
    return 0


if __name__ == "__main__":
    sys.exit(main())
