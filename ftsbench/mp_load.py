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
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import multiprocessing as mp
import time
from typing import Any

from . import corpus_shard, load_driver
from .corpus import batched

CHECKIN_TIMEOUT_S = 300.0
CHECKIN_POLL_S = 0.05


def worker_args(args: argparse.Namespace, index: int,
                workers: int) -> argparse.Namespace:
    """This worker's share of the run's budgets.

    `--concurrency` and `--max-docs` are whole-run numbers, and the artifacts
    compare against them exactly, so the split has to be remainder-preserving
    in both. The worker also carries its own shard identity, because with
    `spawn` a child inherits nothing.
    """
    share = copy.copy(args)
    share.concurrency = max(
        corpus_shard.split_budget(args.concurrency, workers, index), 1)
    share.max_docs = corpus_shard.split_budget(args.max_docs, workers, index)
    share.shard_index = index
    share.shard_count = workers
    return share


def shard_source(args: argparse.Namespace) -> load_driver.Source:
    """The driver's work source, narrowed to this worker's byte range."""
    def source(_args: argparse.Namespace, _origin_s: float):
        documents = corpus_shard.read_shard(
            args.corpus, args.shard_index, args.shard_count, args.max_docs)
        for items in batched(documents, args.batch_size):
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
        "per_worker": results,
    }


def run_sharded(args: argparse.Namespace, engine: str,
                workers: int) -> dict[str, Any]:
    """Load the corpus with `workers` processes and return the run's totals."""
    entrypoint = WORKERS[engine]
    context = mp.get_context("spawn")
    with mp.Manager() as manager:
        queue, condition = manager.Queue(), manager.Condition()
        with concurrent.futures.ProcessPoolExecutor(
                mp_context=context, max_workers=workers) as executor:
            pending = [
                executor.submit(entrypoint, worker_args(args, index, workers),
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
