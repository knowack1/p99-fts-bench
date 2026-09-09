"""N worker processes, each loading a disjoint shard, started together.

The shape is VectorDBBench's, verified against its source: its
`MultiProcessingSearchRunner` runs one `ProcessPoolExecutor` per concurrency
level with `mp.get_context("spawn")`, has every child check in on an
`mp.Queue` and block on an `mp.Condition`, releases them with `notify_all()`,
and only then starts the clock — so client start-up never lands inside the
measured window. Counts and latencies are aggregated in the parent.

Two deliberate differences, both measured rather than assumed:

- **VectorDBBench's own ingest is single-process.** `serial_runner` uses
  `ProcessPoolExecutor(max_workers=1)`, mainly so a hung load can be killed on
  timeout; the process-pool pattern comes from its *search* runner. Applying it
  to ingest is an extension of the concept, not a copy of it.
- **Each worker holds M operations in flight, not one.** VectorDBBench's
  workers issue one blocking query at a time, so concurrency equals process
  count. A ladder reaching c=256 that way would need 256 processes on an 8-vCPU
  harness box. Here `--concurrency` is the run's total offered load and
  `--workers` says how it is split, so the top rungs cost M in-flight operations
  per process rather than a process each.

Processes, because threads were measured to be the ceiling: two threads holding
1,000 outstanding CQL statements delivered 9,024 docs/s where 64 threads holding
64 delivered 2,594 (`results/client-model-2026-09-08/README.md`). Async inside
each process, because the GIL makes a thread per in-flight operation cost more
than it buys.

`--workers auto` asks for the campaign's shape, `N = min(c, N_max)` and
`M = c / N`: N is pinned at its ceiling rather than varied with `c`, because a
knee in a curve where BOTH the process count and the in-flight count moved could
be a change in client architecture rather than an engine effect. Omitting
`--workers` leaves a run exactly as it was before this module was wired in — one
process dispatching in-process through `load_driver`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import multiprocessing as mp
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from . import corpus_shard, load_driver
from .corpus import batched

CHECKIN_TIMEOUT_S = 300.0
CHECKIN_POLL_S = 0.05
WORKERS_AUTO = "auto"
N_MAX_ENV = "LOADER_N_MAX"


class MissingWorkerCeiling(RuntimeError):
    """Raised rather than assuming a worker-process ceiling.

    `N_max` stood at 4 by extrapolation from a per-process ceiling measured with
    the pre-async thread-per-operation client, and Phase 0 of
    `BUILD-RATE-MATRIX-PLAN.md` owes a measured one. A default here would put
    that guess inside every rung of the ladder and publish it as an engine
    result.
    """


@dataclass(frozen=True)
class ClientShape:
    """N worker processes, and the ceiling that decided N.

    `ceiling` rides along so a worker's own artifact can say whether N was
    pinned at the measured ceiling or sits below it because `c < N_max`. The
    rungs below `N_max` necessarily run fewer processes, and a reader has to be
    able to tell which from the files rather than from the chart's footer.
    """

    workers: int
    ceiling: int | None = None


def worker_request(value: str) -> str | int:
    """`--workers` is either the literal `auto` or a fixed positive count."""
    if value == WORKERS_AUTO:
        return WORKERS_AUTO
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError(
            f"--workers is '{WORKERS_AUTO}' or at least 1, got {count}")
    return count


def add_sharding_args(parser: argparse.ArgumentParser,
                      default: str | int | None = None) -> None:
    """The client-shape flags, shared by both loaders and this module's CLI."""
    parser.add_argument("--workers", type=worker_request, default=default,
                        help=f"'{WORKERS_AUTO}' for N = min(--concurrency, "
                             "N_max) worker processes sharing --concurrency "
                             "operations in flight between them, or a fixed "
                             "count; omit for one in-process run")
    parser.add_argument("--n-max", type=int, default=None,
                        help=f"measured worker-process ceiling, or {N_MAX_ENV}; "
                             "no default, deliberately — a guessed ceiling "
                             "would be reported as an engine result")


def _stated_ceiling(args: argparse.Namespace) -> str | int | None:
    if args.n_max is not None:
        return args.n_max
    return os.environ.get(N_MAX_ENV) or None


def worker_ceiling(args: argparse.Namespace) -> int:
    stated = _stated_ceiling(args)
    if stated is None:
        raise MissingWorkerCeiling(
            f"--workers {WORKERS_AUTO} needs a measured process ceiling: pass "
            f"--n-max or set {N_MAX_ENV}. There is no default — see Phase 0 of "
            "BUILD-RATE-MATRIX-PLAN.md.")
    ceiling = int(stated)
    if ceiling < 1:
        raise MissingWorkerCeiling(
            f"worker ceiling must be at least 1, got {ceiling}")
    return ceiling


def client_shape(args: argparse.Namespace) -> ClientShape | None:
    """The run's client shape, or `None` for one in-process load.

    `auto` goes through the process pool even when it resolves to a single
    worker: a ladder whose low rungs ran in-process while its high rungs ran a
    pool would carry a change of client architecture inside the curve, which is
    the confound N is pinned at its ceiling to avoid.
    """
    requested = args.workers
    if requested is None:
        return None
    if requested == WORKERS_AUTO:
        ceiling = worker_ceiling(args)
        return ClientShape(min(args.concurrency, ceiling), ceiling)
    return ClientShape(requested)


def shard_log_path(path: str | None, index: int) -> str | None:
    """One latency artifact per worker.

    `latency_log.open_log` truncates what it opens, so N workers handed one
    path would leave a single shard's records where the run's belong — and a
    percentile taken over 1/N of the operations is not visibly wrong.
    """
    if path is None:
        return None
    root, extension = os.path.splitext(path)
    return f"{root}.shard{index}{extension}"


def worker_args(args: argparse.Namespace, index: int, workers: int,
                ceiling: int | None = None) -> argparse.Namespace:
    """This worker's share of the run's budgets.

    `--concurrency` and `--max-docs` are whole-run numbers, and the artifacts
    compare against them exactly, so the split has to be remainder-preserving
    in both. The worker also carries its own shard identity and its own latency
    artifact, because with `spawn` a child inherits nothing and the whole-run
    values are the ones a reader compares against.
    """
    share = copy.copy(args)
    share.concurrency = max(
        corpus_shard.split_budget(args.concurrency, workers, index), 1)
    share.max_docs = corpus_shard.split_budget(args.max_docs, workers, index)
    share.shard_index = index
    share.shard_count = workers
    share.run_concurrency = args.concurrency
    share.worker_ceiling = ceiling
    share.latency_log = shard_log_path(args.latency_log, index)
    return share


def shard_header_fields(args: argparse.Namespace) -> dict[str, Any]:
    """What a worker's artifact has to say about the client that produced it.

    Empty for a single-process run, so every header written before this module
    was wired in stays byte-identical. `concurrency` in the same header is this
    worker's M, which is only readable next to N and the run's total: the S28
    retraction turned on a header that recorded no concurrency at all, leaving
    the defect unauditable from the files it produced.
    """
    workers = getattr(args, "shard_count", 0)
    if not workers:
        return {}
    fields: dict[str, Any] = {
        "workers": workers,
        "shard": args.shard_index,
        "run_concurrency": args.run_concurrency,
    }
    ceiling = getattr(args, "worker_ceiling", None)
    if ceiling is not None:
        fields["n_max"] = ceiling
    return fields


def shard_source(args: argparse.Namespace) -> load_driver.Source:
    """The driver's work source, narrowed to this worker's byte range."""
    def source(_args: argparse.Namespace, _origin_s: float,
               docs_per_operation: int):
        documents = corpus_shard.read_shard(
            args.corpus, args.shard_index, args.shard_count, args.max_docs)
        for items in batched(documents, docs_per_operation):
            yield load_driver.Batch(items)
    return source


def _check_in_and_wait(queue, condition) -> None:
    """Announce readiness, then block until the parent releases every worker.

    The whole point of the barrier: connecting a client, preparing statements
    and opening sockets takes long enough to matter, and a run that timed it
    would report the slowest worker's start-up as engine latency.
    """
    queue.put(1)
    with condition:
        condition.wait()


def _worker_result(args: argparse.Namespace, log, tally,
                   wall_s: float) -> dict[str, Any]:
    summary = log.summary()
    return {
        "shard": args.shard_index,
        "concurrency": args.concurrency,
        "wall_s": wall_s,
        "ops": summary.get("ops", 0),
        "docs": summary.get("docs", 0),
        "ok_docs": summary.get("ok_docs", 0),
        "errors": summary.get("errors", 0),
        "first_error": summary.get("first_error"),
        "retries": tally.summary(),
    }


def _run_worker(args: argparse.Namespace, loader: load_driver.EngineLoader,
                queue, condition) -> dict[str, Any]:
    source = shard_source(args)
    _check_in_and_wait(queue, condition)
    log, tally, wall_s = load_driver.run_timed(args, loader, source)
    return _worker_result(args, log, tally, wall_s)


def opensearch_worker(args: argparse.Namespace, queue,
                      condition) -> dict[str, Any]:
    """Top-level so `spawn` can pickle it; the client is built in the child,
    because a connection cannot cross a process boundary."""
    from . import opensearch_load

    url = args.url.rstrip("/")
    return _run_worker(args, opensearch_load.build_loader(args, url),
                       queue, condition)


def scylla_worker(args: argparse.Namespace, queue,
                  condition) -> dict[str, Any]:
    from . import scylla_load

    cluster, session = scylla_load.connect(
        args.hosts.split(","), args.port, args.keyspace)
    try:
        statement = scylla_load.prepare_insert(session, args.table)
        return _run_worker(args,
                           scylla_load.build_loader(args, session, statement),
                           queue, condition)
    finally:
        cluster.shutdown()


WORKERS = {"opensearch": opensearch_worker, "scylladb": scylla_worker}


def _await_check_in(queue, workers: int, timeout_s: float) -> None:
    deadline = time.perf_counter() + timeout_s
    while queue.qsize() < workers:
        if time.perf_counter() > deadline:
            raise TimeoutError(
                f"only {queue.qsize()} of {workers} workers checked in within "
                f"{timeout_s:.0f}s; a client failed to connect")
        time.sleep(CHECKIN_POLL_S)


def aggregate(results: list[dict[str, Any]], wall_s: float) -> dict[str, Any]:
    """The run's numbers, from the parent's clock.

    Throughput divides by the PARENT's wall, not by any worker's: workers finish
    at different times, and summing per-worker rates would report a rate the run
    never sustained. `ok_docs` rather than `docs`, because dividing by documents
    the engine rejected reports refused work as delivered throughput.
    """
    ok_docs = sum(r["ok_docs"] for r in results)
    return {
        "workers": len(results),
        "concurrency": sum(r["concurrency"] for r in results),
        "wall_s": round(wall_s, 3),
        "ops": sum(r["ops"] for r in results),
        "docs": sum(r["docs"] for r in results),
        "ok_docs": ok_docs,
        "errors": sum(r["errors"] for r in results),
        "docs_per_s": round(ok_docs / wall_s, 1) if wall_s > 0 else 0.0,
        **_retry_totals(results),
        "per_worker": results,
    }


def _retry_totals(results: list[dict[str, Any]]) -> dict[str, int]:
    return {field: sum(r["retries"].get(field, 0) for r in results)
            for field in ("retried_items", "retries")}


def summary_line(summary: dict[str, Any]) -> str:
    return (f"{summary['workers']} worker(s), c={summary['concurrency']} | "
            f"{summary['ops']} ops, {summary['docs']} docs, "
            f"{summary['ok_docs']} ok, {summary['errors']} failed | "
            f"{summary['docs_per_s']} docs/s over {summary['wall_s']}s")


def retries_line(summary: dict[str, Any]) -> str:
    """The run's retry disclosure, worded from the totals.

    A `RetryTally` cannot be replayed from per-worker counts — `note` counts one
    retry per call — and a retry is a condition of the run either way.
    """
    if not summary["retries"]:
        return "no retries"
    return (f"{summary['retried_items']} item(s) retried over "
            f"{summary['retries']} retries in {summary['workers']} worker(s) "
            "— the client was overloaded")


def warn_if_capped_per_shard(args: argparse.Namespace, workers: int) -> None:
    if args.max_docs and workers > 1:
        print(f"WARNING: --max-docs {args.max_docs} is split across {workers} "
              "workers, so this run loads max_docs/N documents from the HEAD "
              "of each shard — a different SET of documents than the first "
              "--max-docs of the corpus, and not comparable with an unsharded "
              "capped run. See BUILD-RATE-MATRIX-PLAN.md.", file=sys.stderr)


def run_sharded(args: argparse.Namespace, engine: str,
                shape: ClientShape) -> dict[str, Any]:
    """Load the corpus with `shape.workers` processes and return the totals."""
    entrypoint = WORKERS[engine]
    workers = shape.workers
    warn_if_capped_per_shard(args, workers)
    context = mp.get_context("spawn")
    with mp.Manager() as manager:
        queue, condition = manager.Queue(), manager.Condition()
        with concurrent.futures.ProcessPoolExecutor(
                mp_context=context, max_workers=workers) as executor:
            pending = [
                executor.submit(entrypoint,
                                worker_args(args, index, workers,
                                            shape.ceiling),
                                queue, condition)
                for index in range(workers)
            ]
            _await_check_in(queue, workers, CHECKIN_TIMEOUT_S)
            with condition:
                condition.notify_all()
            started = time.perf_counter()
            results = [future.result() for future in pending]
            wall_s = time.perf_counter() - started
    return aggregate(results, wall_s)


def parse_args() -> argparse.Namespace:
    """This module's own front door: one command launching N workers.

    Both loaders' engine flags are registered, and imported here rather than at
    module scope: they import this module for `add_sharding_args`, so a
    top-level import would close the cycle.
    """
    from . import opensearch_load, scylla_load

    parser = argparse.ArgumentParser(description=__doc__)
    load_driver.add_common_args(parser, batch_size=False)
    parser.add_argument("--batch-size", type=int, default=None,
                        help="documents per operation; OpenSearch only, where "
                             "one operation is one _bulk")
    add_sharding_args(parser, default=WORKERS_AUTO)
    parser.add_argument("--engine", required=True, choices=sorted(WORKERS),
                        help="which loader every worker runs")
    opensearch_load.add_engine_args(parser)
    scylla_load.add_engine_args(parser)
    args = parser.parse_args()
    refuse_batch_size_without_a_batch(parser, args)
    return args


def refuse_batch_size_without_a_batch(parser: argparse.ArgumentParser,
                                      args: argparse.Namespace) -> None:
    """This module's CLI serves both engines, so it must register the flag one
    of them has. Accepting it for ScyllaDB and ignoring it would be worse than
    rejecting it: the label and the manifest would say a batch size the run
    never had, which is the S28 failure — complete, plausible artifacts
    describing a shape nobody ran."""
    if args.engine == "opensearch" and args.batch_size is None:
        parser.error("--batch-size is required for --engine opensearch")
    if args.engine != "opensearch" and args.batch_size is not None:
        parser.error(
            f"--batch-size is meaningless for --engine {args.engine}: there is "
            f"no wire batch, one operation is one prepared INSERT, and "
            f"--concurrency is the only knob")


def main() -> int:
    args = parse_args()
    load_driver.warn_if_client_bound(args.concurrency)
    summary = run_sharded(args, args.engine, client_shape(args))
    print(f"{args.engine} sharded load: {summary_line(summary)} "
          f"({retries_line(summary)})", file=sys.stderr)
    print(json.dumps(summary))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
