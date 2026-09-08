"""Shared dispatch for both ingest paths.

The two engines must differ in what they do with a batch, never in how the
client offers it. Before this module they differed in both: `opensearch_load`
ran a `ThreadPoolExecutor` holding N `_bulk` requests in flight, while
`scylla_load` dispatched one batch at a time on the calling thread and let the
driver hold N *rows* in flight inside it. Two consequences, both of which
reached published numbers:

- **`--concurrency` named different quantities**, so no chart could compare the
  engines at equal x, and every build-rate chart had to carry a footer saying
  so;
- **all of `scylla_load`'s encoding ran on one thread**, capping it near
  9.8k docs/s against `opensearch_load`'s ~11.4k. Because OpenSearch's client
  ceiling sat *above* its engine and ScyllaDB's sat *below* its engine, one side
  measured its engine and the other measured its client. That is how a 1.28x
  "OpenSearch is faster" result was published from a difference in client
  architecture.

The read path never had this problem — `engines.py` exposes `search()` per
engine and one driver calls it — so this brings ingest to the shape the query
side already had.

Here the driver owns the schedule, the in-flight bound, the timing and the
retry accounting. An engine supplies only:

- ``encode(batch) -> payload``, run on the **dispatcher** thread so that batch k
  holds the same documents at every concurrency and `service_ms` stays
  transport-only;
- ``send(payload, tally)``, run on a **worker** thread — the one place the two
  engines are allowed to differ.

`--concurrency` now means *operations in flight* on both sides.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent import futures
from dataclasses import dataclass, field
from typing import Any

from . import latency_log, load_retry, pacer, runmeta
from .corpus import batched, read_corpus
from .progress import ThroughputReporter


@dataclass(frozen=True)
class Batch:
    """One operation's worth of work, as the SOURCE built it.

    `op_kind` rides with the batch rather than with the loader because a churn
    stream changes kind as its ring fills — add-only during warm-in, add and
    delete after — and a per-loader `op_kind` cannot say so. `None` means "use
    the loader's", which is what keeps every existing C1/C3 artifact's `op`
    field byte-identical.
    """

    items: list
    op_kind: str | None = None


# The work source is a property of the RUN, not of the engine, which is why it
# is a parameter of `run` and not a field of `EngineLoader`. `EngineLoader` is
# built per engine; a source living there would let the two engines feed
# themselves different work, and "the engines were offered the same work in the
# same order" is the one property this module exists to guarantee.
Source = Callable[[argparse.Namespace, float], Iterable[Batch]]


@dataclass(frozen=True)
class EngineLoader:
    """What an ingest path must supply beyond the shared machinery."""

    name: str
    engine: str
    op_kind: str
    engine_version: str
    encode: Callable[[list[dict]], Any]
    send: Callable[[Any, load_retry.RetryTally], None]
    header_fields: dict[str, Any] = field(default_factory=dict)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Flags whose meaning is identical on both sides.

    Kept in one place because a flag that drifts apart between the two loaders
    is exactly the defect this module exists to remove.
    """
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--batch-size", type=int, required=True,
                        help="documents per operation; must match across "
                             "engines for any per-operation latency comparison")
    parser.add_argument("--concurrency", type=int, required=True,
                        help="operations in flight — the same quantity on both "
                             "engines")
    parser.add_argument("--target-rate", type=float, default=0.0,
                        help="offered ingest rate in docs/s; 0 = unpaced "
                             "closed-loop dispatch, where latency_ms equals "
                             "service_ms and is not an SLA latency")
    parser.add_argument("--latency-log", default=None,
                        help="write one latency_op record per operation (C3)")
    parser.add_argument("--label", default="",
                        help="free-form run label recorded in the header")
    parser.add_argument("--cache-state", default="unspecified",
                        help="cold|warm|unspecified — recorded for the footer")
    parser.add_argument("--max-docs", type=int, default=0, help="0 = no cap")


def warn_if_client_bound(concurrency: int) -> None:
    if concurrency <= 1:
        print("WARNING: --concurrency 1 offers one operation at a time; the "
              "measured rate is the client's, not the engine's, and not "
              "quotable. See TUNING.md.", file=sys.stderr)


def _surface_errors(done: set[futures.Future]) -> None:
    """A worker exception is a harness bug: an operation the engine rejected is
    recorded by `timed_op` and never raises, so anything arriving here must not
    be absorbed into a plausible-looking series."""
    for future in done:
        future.result()


class InFlightPool:
    """Bounded set of in-flight operations.

    Bounded on purpose. An unbounded submit queue under a paced run would
    absorb the backlog into client memory and hide it from `queue_ms` — and
    `queue_ms` is the number that tells a reader the generator, not the engine,
    was the bottleneck.
    """

    def __init__(self, executor: futures.ThreadPoolExecutor,
                 max_inflight: int) -> None:
        self._executor = executor
        self._max_inflight = max_inflight
        self._pending: set[futures.Future] = set()

    def submit(self, work: Callable[[], None]) -> None:
        self._await_capacity()
        self._pending.add(self._executor.submit(work))

    def drain(self) -> None:
        _surface_errors(futures.wait(self._pending).done)
        self._pending = set()

    def _await_capacity(self) -> None:
        while len(self._pending) >= self._max_inflight:
            done, self._pending = futures.wait(
                self._pending, return_when=futures.FIRST_COMPLETED)
            _surface_errors(done)


def _operation(log: latency_log.LatencyLog, op: pacer.Op,
               loader: EngineLoader, payload: Any, batch: Batch,
               tally: load_retry.RetryTally) -> Callable[[], None]:
    """Each worker times its own operation against the run's shared origin."""
    op_kind = batch.op_kind or loader.op_kind
    n_docs = len(batch.items)

    def work() -> None:
        latency_log.timed_op(log, op.i, op.t_intended_s, op_kind, n_docs,
                             lambda: loader.send(payload, tally))
    return work


def corpus_batches(args: argparse.Namespace,
                   origin_s: float) -> Iterator[Batch]:
    """The default source: replay the corpus once, in order.

    `origin_s` is unused here because a corpus replay ends when the corpus
    does. It stays in the signature so the driver remains the single owner of
    the run origin — a source that needs a deadline must be handed the same
    clock zero every `t_*_s` is relative to, never stamp its own.
    """
    for items in batched(read_corpus(args.corpus, args.max_docs),
                         args.batch_size):
        yield Batch(items)


@dataclass(frozen=True)
class Dispatch:
    """Everything one dispatch loop needs, so `_dispatch` stays readable."""

    pool: InFlightPool
    log: latency_log.LatencyLog
    args: argparse.Namespace
    origin_s: float
    tally: load_retry.RetryTally


def _header(args: argparse.Namespace, loader: EngineLoader) -> dict[str, Any]:
    return runmeta.header(
        producer=f"{loader.name}_load", engine=loader.engine,
        engine_version=loader.engine_version,
        label=args.label, cache_state=args.cache_state, corpus=args.corpus,
        max_docs=args.max_docs, batch_size=args.batch_size,
        concurrency=args.concurrency,
        concurrency_unit="operations in flight",
        target_rate_docs_per_s=args.target_rate,
        retry_attempts=load_retry.DEFAULT_POLICY.attempts,
        **loader.header_fields,
    )


def _dispatch(context: Dispatch, loader: EngineLoader,
              source: Source) -> None:
    schedule = latency_log.op_schedule(context.args.target_rate,
                                       context.args.batch_size,
                                       context.origin_s)
    reporter = ThroughputReporter(f"{loader.engine} load")
    for batch in source(context.args, context.origin_s):
        payload = loader.encode(batch.items)
        op = next(schedule)
        context.pool.submit(_operation(context.log, op, loader, payload, batch,
                                       context.tally))
        reporter.add(len(batch.items))
    context.pool.drain()
    reporter.finish()


def run_timed(args: argparse.Namespace, loader: EngineLoader,
              source: Source = corpus_batches
              ) -> tuple[latency_log.LatencyLog, load_retry.RetryTally, float]:
    """`run`, plus the wall an achieved rate must be divided by.

    The wall is stamped here, by the code that owns `origin_s`, and only after
    the pool has drained and every worker has been joined. A producer that
    measured its own wall could divide completions by a window the driver never
    ran, which is how a rate becomes a submission rate without anyone deciding
    that it should.
    """
    origin_s = time.perf_counter()
    tally = load_retry.RetryTally()
    with latency_log.open_log(args.latency_log, _header(args, loader), origin_s) as log, \
            futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        _dispatch(Dispatch(InFlightPool(executor, args.concurrency), log, args,
                           origin_s, tally), loader, source)
    return log, tally, time.perf_counter() - origin_s


def run(args: argparse.Namespace, loader: EngineLoader,
        source: Source = corpus_batches) -> tuple[latency_log.LatencyLog,
                                                  load_retry.RetryTally]:
    log, tally, _ = run_timed(args, loader, source)
    return log, tally


def append_record(path: str, record: dict[str, Any]) -> None:
    """Add a producer's closing record to the artifact the driver has already
    written the header of, so a producer that needs a summary appends to one
    file rather than opening a second."""
    with open(path, "a", encoding="utf-8") as stream:
        runmeta.write_record(stream, record)
