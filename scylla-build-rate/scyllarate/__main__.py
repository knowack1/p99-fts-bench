"""python3 -m scyllarate --corpus ../data/corpus.jsonl --concurrency 8,16,32,64,128

Measures how fast this client can submit prepared INSERTs to ScyllaDB, per
concurrency level. That is a submit rate, not an FTS index build rate: a
completed CQL write says nothing about how many documents reached the index.
"""
import argparse
import asyncio
import sys

from cassandra import ConsistencyLevel

from . import cli, report, session, sweep
from .corpus import read_insert_params


def _main() -> int:
    args = cli.build_parser().parse_args()
    cluster = session.build_cluster(args.hosts, args.port, args.consistency,
                                    args.request_timeout, args.executor_threads)
    try:
        results = _measure(cluster, args)
    finally:
        cluster.shutdown()
    return _exit_code(results)


def _measure(cluster, args: argparse.Namespace) -> list[report.PointResult]:
    live = session.connect(cluster, args.keyspace)
    statement = session.prepare_insert(live, args.table)
    topology = session.read_topology(cluster, live, args.keyspace, args.table)
    _describe(topology)
    results = asyncio.run(sweep.run_sweep(
        live, statement, _source_factory(args), args.concurrency))
    _publish(topology, args, results)
    return results


def _source_factory(args: argparse.Namespace) -> sweep.SourceFactory:
    return lambda: read_insert_params(args.corpus, args.max_docs)


def _describe(topology: session.Topology) -> None:
    report.note(f"scylla {topology.scylla_version}, driver {topology.driver_version}, "
                f"protocol {topology.protocol_version}, {topology.reactor}")
    report.note(f"shard_aware={topology.shard_aware} shards={topology.shards} "
                f"tablets={topology.tablets}")


def _publish(topology: session.Topology, args: argparse.Namespace,
            results: list[report.PointResult]) -> None:
    text = report.csv_text(topology, _settings(args), results)
    report.write_csv(text, args.out)
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


def _exit_code(results: list[report.PointResult]) -> int:
    return 1 if any(result.errors for result in results) else 0


if __name__ == "__main__":
    sys.exit(_main())
