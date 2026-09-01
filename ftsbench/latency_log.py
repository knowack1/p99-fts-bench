"""Per-operation latency records — the `latency_op` series behind chart C3.

C3 asks the question C1 cannot answer. C1 reports how many documents per second
an engine absorbs; C3 reports what one write *cost* while the engine was busy
doing its own background work. A throughput average hides a two-second Lucene
merge stall or a Tantivy commit completely, because the seconds either side of
it are fast enough to pay for it. A per-operation tail cannot hide it, which is
why every ingest operation gets its own record here.

Four decisions this module exists to enforce:

- **Latency is measured from the intended start**, never from the moment the
  send actually began, so a stalled engine cannot make itself look fast by
  being asked fewer questions while it stalls. `ftsbench.pacer` owns that
  arithmetic; this module only reports what it returns, and also reports
  `queue_ms` so a reader can tell an engine tail from a generator tail.
- **Failed operations are recorded, never dropped.** An engine that starts
  rejecting writes under merge pressure would otherwise appear as an engine
  with a shorter tail. Failures carry `ok: false` plus the message, are
  excluded from the percentiles, counted in the error rate, and printed as they
  happen — a broken run must be loud rather than merely short.
- **Every record is flushed.** A run killed mid-ingest — an OOM-kill, a
  laptop's swap death — must leave the window it did cover usable.
- **The unpaced case is labelled by its own numbers.** With `--target-rate 0`
  dispatch is closed-loop, the intended time *is* now, and `latency_ms` equals
  `service_ms` by construction. That is the right measurement for a
  maximum-throughput C1 build and must never be presented as a latency SLA.

The module carries the dispatch schedule for the loaders too (`op_schedule`),
because both loaders need exactly the same choice between paced and closed-loop
dispatch and a second copy of that choice is a second place for it to be wrong.
"""
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TextIO

from . import pacer, runmeta, stats

SCHEDULE_CHUNK_OPS = 4096
QUEUE_PERCENTILE = 99


@dataclass(frozen=True)
class OpTiming:
    """Absolute `time.perf_counter()` stamps for one operation."""
    i: int
    t_intended_s: float
    t_start_s: float
    t_end_s: float

    def relative_to(self, origin_s: float) -> "OpTiming":
        """SCHEMAS.md requires `t_*_s` as seconds from run start, so the run's
        single shared origin is subtracted once, here, and not by each caller."""
        return OpTiming(
            i=self.i,
            t_intended_s=self.t_intended_s - origin_s,
            t_start_s=self.t_start_s - origin_s,
            t_end_s=self.t_end_s - origin_s,
        )


def build_latency_op(timing: OpTiming, latencies: tuple[float, float, float],
                     op: str, n_docs: int | None = None,
                     query_class: str | None = None, query_i: int | None = None,
                     hits: int | None = None, ok: bool = True,
                     error: str | None = None) -> dict[str, Any]:
    """The one `latency_op` field list, shared by the ingest loaders (through
    LatencyLog) and the query load generator (through load_gen).

    `timing` must already be relative to the run origin. Kept in a single place
    on purpose: two independent copies of this field list would be the one spot
    where the ingest and query harnesses could drift apart on schema, and a
    drifted field silently changes what C3, C5 and C6 are computed from without
    failing anything.
    """
    latency_ms, service_ms, queue_ms = latencies
    return {
        "record": "latency_op",
        "i": timing.i,
        "t_intended_s": round(timing.t_intended_s, 6),
        "t_start_s": round(timing.t_start_s, 6),
        "t_end_s": round(timing.t_end_s, 6),
        "latency_ms": round(latency_ms, 3),
        "service_ms": round(service_ms, 3),
        "queue_ms": round(queue_ms, 3),
        "op": op,
        "n_docs": n_docs,
        "class": query_class,
        "query_i": query_i,
        "hits": hits,
        "ok": ok,
        "error": error,
    }


def _undefined_p99_note(summary: dict[str, Any]) -> str:
    """A short ingest run has too few batches for its own p99 to mean anything;
    `stats` knows the floor, so say so next to the number rather than beside it
    in a document nobody reads."""
    if 99.0 not in (summary.get("unsupported_percentiles") or []):
        return ""
    return f" (p99 undefined at {summary['count']} operations)"


def _report_failure(fields: dict[str, Any]) -> None:
    print(f"op {fields['i']} ({fields['op']}, n_docs={fields['n_docs']}) failed "
          f"after {fields['latency_ms']:.1f} ms: {fields['error']}",
          file=sys.stderr)


class LatencyLog:
    """Writes `latency_op` records and accumulates the run's own summary.

    Safe to call from several worker threads: the loaders time each operation on
    the worker that ran it, so records arrive in completion order while `i` is
    dispatch order. A consumer must therefore sort on `t_*_s` and never rely on
    file order.

    A `None` stream still accumulates. That is what lets an unpaced C1 run print
    its own service-time tail without emitting a C3 artifact nobody asked for.
    """

    def __init__(self, stream: TextIO | None, origin_s: float) -> None:
        self._stream = stream
        self._origin_s = origin_s
        self._lock = threading.Lock()
        self._ops = 0
        self._docs = 0
        self._errors = 0
        self._latencies_ms: list[float] = []
        self._queue_ms: list[float] = []

    def record(self, timing: OpTiming, op: str, n_docs: int | None = None,
               query_class: str | None = None, query_i: int | None = None,
               hits: int | None = None, ok: bool = True,
               error: str | None = None) -> None:
        relative = timing.relative_to(self._origin_s)
        latencies = pacer.latencies_ms(relative.t_intended_s,
                                       relative.t_start_s, relative.t_end_s)
        fields = build_latency_op(relative, latencies, op, n_docs=n_docs,
                                  query_class=query_class, query_i=query_i,
                                  hits=hits, ok=ok, error=error)
        with self._lock:
            self._accumulate(latencies, n_docs or 0, ok)
            self._emit(fields)
        if not ok:
            _report_failure(fields)

    def summary(self) -> dict[str, Any]:
        """`ops` counts every operation; the percentiles cover only the ones
        that succeeded, per SCHEMAS.md."""
        with self._lock:
            return {
                "ops": self._ops,
                "docs": self._docs,
                "errors": self._errors,
                **self._latency_summary(),
            }

    def summary_line(self) -> str:
        summary = self.summary()
        if "p50_ms" not in summary:
            return (f"{summary['ops']} ops, {summary['errors']} failed, "
                    "no successful operation to take a percentile of")
        return (f"{summary['ops']} ops, {summary['docs']} docs, "
                f"{summary['errors']} failed | latency_ms p50 {summary['p50_ms']} "
                f"p99 {summary['p99_ms']} max {summary['max_ms']} | "
                f"queue_ms p99 {summary['queue_p99_ms']}"
                f"{_undefined_p99_note(summary)}")

    def _accumulate(self, latencies: tuple[float, float, float], n_docs: int,
                    ok: bool) -> None:
        latency_ms, _, queue_ms = latencies
        self._ops += 1
        self._docs += n_docs
        if not ok:
            self._errors += 1
            return
        self._latencies_ms.append(latency_ms)
        self._queue_ms.append(queue_ms)

    def _emit(self, fields: dict[str, Any]) -> None:
        if self._stream is not None:
            runmeta.write_record(self._stream, fields)

    def _latency_summary(self) -> dict[str, Any]:
        if not self._latencies_ms:
            return {}
        return {
            **stats.summarize_latencies(self._latencies_ms),
            "queue_p99_ms": round(
                stats.percentile(sorted(self._queue_ms), QUEUE_PERCENTILE), 3),
        }


@contextmanager
def open_log(path: str | None, header: dict[str, Any],
             origin_s: float) -> Iterator[LatencyLog]:
    """Open a `latency_op` artifact, header first. Without a path the log still
    summarises but writes nothing."""
    if path is None:
        yield LatencyLog(None, origin_s)
        return
    with open(path, "w", encoding="utf-8") as stream:
        runmeta.write_record(stream, header)
        yield LatencyLog(stream, origin_s)


def timed_op(log: LatencyLog, op_i: int, t_intended_s: float, op: str,
             n_docs: int, action: Callable[[], None]) -> None:
    """Run one ingest operation and record it, whether or not it succeeded.

    The broad except is the point: a write that the engine rejected is a data
    point about the engine, and losing it would turn a failing engine into a
    fast one. `LatencyLog.record` prints it, so it is recorded and surfaced
    rather than swallowed.
    """
    t_start_s = time.perf_counter()
    ok, error = True, None
    try:
        action()
    except Exception as err:
        ok, error = False, f"{type(err).__name__}: {err}"
    t_end_s = time.perf_counter()
    log.record(OpTiming(op_i, t_intended_s, t_start_s, t_end_s), op=op,
               n_docs=n_docs, ok=ok, error=error)


def _renumbered(ops: Iterator[pacer.Op], first_i: int) -> Iterator[pacer.Op]:
    for op in ops:
        yield pacer.Op(i=first_i + op.i, t_intended_s=op.t_intended_s)


def _chunk_ops(target_docs_per_s: float, batch_size: int, origin_s: float,
               first_i: int) -> Iterator[pacer.Op]:
    if target_docs_per_s <= 0:
        return pacer.unpaced(SCHEDULE_CHUNK_OPS)
    op_rate = target_docs_per_s / batch_size
    return pacer.paced(op_rate, SCHEDULE_CHUNK_OPS,
                       origin_s + first_i / op_rate)


def op_schedule(target_docs_per_s: float, batch_size: int,
                origin_s: float) -> Iterator[pacer.Op]:
    """Unbounded dispatch schedule for ingest operations, in batches.

    Paced when a target rate is set, because C3 needs a *controlled* offered
    rate: at saturation the recorded latencies are queueing delay and the chart
    would be measuring the backlog rather than the engine. Closed-loop when the
    rate is 0, which is what C1's maximum-throughput build wants.

    Chunked because `pacer.schedule` materialises its offsets and the number of
    batches is not known until the corpus ends. Each chunk is anchored on the
    absolute origin, so chunking cannot make the schedule drift.
    """
    first_i = 0
    while True:
        yield from _renumbered(
            _chunk_ops(target_docs_per_s, batch_size, origin_s, first_i),
            first_i)
        first_i += SCHEDULE_CHUNK_OPS
