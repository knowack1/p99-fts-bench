"""python3 -m scyllarate --corpus ../data/corpus.jsonl --concurrency 8,16,32,64,128

Measures how fast this client can submit prepared INSERTs to ScyllaDB, per
concurrency level. That is a submit rate, not an FTS index build rate: a
completed CQL write says nothing about how many documents reached the index.
"""
import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import TextIO

from cassandra import ConsistencyLevel

from . import cli, report, session, sweep
from .corpus import read_insert_params


def _main() -> int:
    args = cli.build_parser().parse_args()
    cluster = session.build_cluster(args.hosts, args.port, args.consistency,
                                    args.request_timeout, args.executor_threads)
    try:
        results, aborted = _measure(cluster, args)
    finally:
        cluster.shutdown()
    return _exit_code(results, aborted)


def _measure(cluster, args: argparse.Namespace
             ) -> tuple[list[report.PointResult], bool]:
    live = session.connect(cluster, args.keyspace)
    statement = session.prepare_insert(live, args.table)
    topology = session.read_topology(cluster, live, args.keyspace, args.table)
    _describe(topology)
    results: list[report.PointResult] = []
    with report.open_csv(args.out) as handle:
        report.write_preamble(handle, topology, _settings(args))
        aborted = _sweep(live, statement, args, _collector(results, handle))
    _echo_summary(results)
    return results, aborted


def _sweep(live, statement, args: argparse.Namespace,
           on_point: sweep.OnPoint) -> bool:
    """`KeyboardInterrupt` is caught alongside `Exception` because Ctrl-C is
    the ordinary way a long ladder ends early, and the levels already measured
    are worth as much then as after a driver error."""
    try:
        asyncio.run(sweep.run_sweep(live, statement, _source_factory(args),
                                    args.concurrency, on_point))
    except (Exception, KeyboardInterrupt) as exc:
        _announce_abort(exc, args.out)
        return True
    return False


def _collector(results: list[report.PointResult],
               handle: TextIO) -> Callable[[report.PointResult], None]:
    def keep(result: report.PointResult) -> None:
        results.append(result)
        report.append_row(handle, result)
    return keep


def _announce_abort(exc: Exception, destination: str) -> None:
    report.note(f"!! sweep aborted: {type(exc).__name__}: {exc}")
    report.note(f"!! the levels measured before it are in {destination}")


def _source_factory(args: argparse.Namespace) -> sweep.SourceFactory:
    return lambda: read_insert_params(args.corpus, args.max_docs)


def _describe(topology: session.Topology) -> None:
    report.note(f"scylla {topology.scylla_version}, driver {topology.driver_version}, "
                f"protocol {topology.protocol_version}, {topology.reactor}")
    report.note(f"shard_aware={topology.shard_aware} shards={topology.shards} "
                f"tablets={topology.tablets}")


def _echo_summary(results: list[report.PointResult]) -> None:
    report.note("")
    report.note(report.summary_table(results))


def _settings(args: argparse.Namespace) -> dict[str, str]:
    return {
        "consistency": ConsistencyLevel.value_to_name[args.consistency],
        "request_timeout_s": str(args.request_timeout),
        "executor_threads": str(args.executor_threads),
        "corpus": args.corpus,
        "max_docs": str(args.max_docs),
    }


def _exit_code(results: list[report.PointResult], aborted: bool) -> int:
    return 1 if aborted or any(result.errors for result in results) else 0


if __name__ == "__main__":
    sys.exit(_main())
