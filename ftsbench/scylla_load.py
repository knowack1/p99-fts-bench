"""Load the canonical corpus into ScyllaDB with prepared, concurrent INSERTs.

Idempotent: article_id is the corpus's deterministic uuid5 of the page id, so
re-running overwrites the same rows. Apply scylladb/schema.cql first; create
the fulltext index before or after loading depending on which ingest path
(CDC tail vs. bootstrap scan) the run is meant to exercise.

The measured operation is one *batch* — `--batch-size` rows dispatched through
`execute_concurrent_with_args` at `--concurrency` in flight — and its latency is
recorded once the whole batch has landed. That is the same granularity as
OpenSearch's `_bulk`, so C3 is comparable per operation only when both loaders
are given the same `--batch-size`. The two `--concurrency` flags are *not* the
same quantity: here it is rows in flight within one batch, there it is whole
`_bulk` requests in flight. See `TUNING.md`.

Usage: python3 -m ftsbench.scylla_load --corpus data/corpus.jsonl --hosts 127.0.0.1
"""
import argparse
import sys
import time
import uuid
from functools import partial
from typing import Any

from . import latency_log, load_retry, runmeta
from .corpus import batched, read_corpus
from .progress import ThroughputReporter

DEFAULT_HOSTS = "127.0.0.1"
DEFAULT_PORT = 9042
DEFAULT_KEYSPACE = "wiki"
DEFAULT_TABLE = "articles"
DEFAULT_BATCH_DOCS = 1_000
DEFAULT_CONCURRENCY = 128


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--hosts", default=DEFAULT_HOSTS, help="comma-separated contact points")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keyspace", default=DEFAULT_KEYSPACE)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_DOCS)
    parser.add_argument("--unlogged-batch-rows", type=int, default=0,
                        help="DIAGNOSTIC: group this many rows per UNLOGGED "
                             "BATCH (0 = per-row prepared statements, the "
                             "default and the only mode fit for a published "
                             "write number). See attempt_unlogged_batches.")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help="rows in flight within one batch (driver-internal; "
                             "not the same quantity as opensearch_load's flag)")
    parser.add_argument("--target-rate", type=float, default=0.0,
                        help="offered ingest rate in docs/s; 0 = unpaced "
                             "closed-loop dispatch, where latency_ms equals "
                             "service_ms and is not an SLA latency")
    parser.add_argument("--latency-log", default=None,
                        help="write one latency_op record per batch (chart C3)")
    parser.add_argument("--label", default="", help="free-form run label recorded in the header")
    parser.add_argument("--cache-state", default="unspecified",
                        help="cold|warm|unspecified — recorded for the chart footer")
    parser.add_argument("--max-docs", type=int, default=0, help="0 = no cap")
    return parser.parse_args()


def connect(hosts: list[str], port: int, keyspace: str):
    from cassandra.cluster import Cluster

    cluster = Cluster(hosts, port=port)
    return cluster, cluster.connect(keyspace)


def engine_version(session) -> str:
    try:
        return str(session.execute(
            "SELECT release_version FROM system.local").one().release_version)
    except Exception:
        return "unknown"


def prepare_insert(session, table: str):
    return session.prepare(
        f"INSERT INTO {table} (article_id, page_id, title, body) VALUES (?, ?, ?, ?)"
    )


def insert_parameters(batch: list[dict]) -> list[tuple]:
    return [
        (uuid.UUID(doc["uuid"]), doc["id"], doc["title"], doc["text"])
        for doc in batch
    ]


def concurrent_results(session, statement, parameters: list[tuple],
                       concurrency: int) -> list:
    """`raise_on_first_error=False` so every row is attempted and the per-row
    outcomes come back. Raising on the first one abandoned the rest of the batch
    unsent, which is how a single ConnectionBusy cost one repetition 380
    documents — see ftsbench.load_retry."""
    from cassandra.concurrent import execute_concurrent_with_args

    return execute_concurrent_with_args(
        session, statement, parameters,
        concurrency=concurrency, raise_on_first_error=False,
    )


def attempt_rows(session, statement, concurrency: int,
                 parameters: list[tuple]) -> load_retry.Attempt:
    """Driver results are (success, result-or-exception) in the order sent."""
    results = concurrent_results(session, statement, parameters, concurrency)
    failed = [row for row, outcome in zip(parameters, results) if not outcome[0]]
    error = next((f"{type(outcome[1]).__name__}: {outcome[1]}"
                  for outcome in results if not outcome[0]), "")
    return load_retry.Attempt(failed, error)


def execute_batch(session, statement, parameters: list[tuple],
                  concurrency: int, tally: load_retry.RetryTally) -> None:
    load_retry.send_with_retries(
        parameters, partial(attempt_rows, session, statement, concurrency), tally)


def attempt_unlogged_batches(session, statement, batch_rows: int, concurrency: int,
                             parameters: list[tuple]) -> load_retry.Attempt:
    """UNLOGGED BATCH, in groups of `batch_rows`, `concurrency` batches in flight.

    Diagnostic mode only. Every row here is its own partition, so a multi-row
    batch is the documented anti-pattern: the coordinator fans out to every
    partition's replica set and shard-aware routing is defeated. ScyllaDB logs a
    warning per batch and returns WriteTimeout if pushed too hard.

    It exists because the default per-row path is bound by this client's GIL at
    roughly 11k docs/s, which is below rates the index side can be pushed to.
    Measuring the index with the loader as the binding constraint measures the
    loader. Use this to create headroom, never to produce a published write
    number.
    """
    from cassandra.query import BatchStatement, BatchType

    pending, failed, error = [], [], ""
    for start in range(0, len(parameters), batch_rows):
        rows = parameters[start:start + batch_rows]
        batch = BatchStatement(batch_type=BatchType.UNLOGGED)
        for row in rows:
            batch.add(statement, row)
        pending.append((session.execute_async(batch), rows))
        if len(pending) >= concurrency:
            failed_now, error_now = drain(pending)
            failed += failed_now
            error = error or error_now
            pending = []
    failed_now, error_now = drain(pending)
    return load_retry.Attempt(failed + failed_now, error or error_now)


def drain(pending: list) -> tuple[list[tuple], str]:
    failed, error = [], ""
    for future, rows in pending:
        try:
            future.result()
        except Exception as exc:
            failed += rows
            error = error or f"{type(exc).__name__}: {exc}"
    return failed, error


def execute_unlogged(session, statement, parameters: list[tuple], batch_rows: int,
                     concurrency: int, tally: load_retry.RetryTally) -> None:
    load_retry.send_with_retries(
        parameters,
        partial(attempt_unlogged_batches, session, statement, batch_rows, concurrency),
        tally)


def load_header(args: argparse.Namespace, session) -> dict[str, Any]:
    return runmeta.header(
        producer="scylla_load", engine="scylladb",
        engine_version=engine_version(session),
        label=args.label, cache_state=args.cache_state, corpus=args.corpus,
        max_docs=args.max_docs, keyspace=args.keyspace, table=args.table,
        batch_size=args.batch_size, concurrency=args.concurrency,
        target_rate_docs_per_s=args.target_rate,
        unlogged_batch_rows=args.unlogged_batch_rows,
        retry_attempts=load_retry.DEFAULT_POLICY.attempts,
    )


def batch_sender(args: argparse.Namespace, session, statement, batch, tally):
    rows = insert_parameters(batch)
    if args.unlogged_batch_rows:
        return partial(execute_unlogged, session, statement, rows,
                       args.unlogged_batch_rows, args.concurrency, tally)
    return partial(execute_batch, session, statement, rows, args.concurrency, tally)


def dispatch_all(args: argparse.Namespace, session, statement,
                 log: latency_log.LatencyLog, origin_s: float,
                 tally: load_retry.RetryTally) -> None:
    """One batch at a time on this thread: `execute_concurrent_with_args` already
    holds `--concurrency` rows in flight, so a second layer of client threads
    would make the recorded in-flight depth unstateable."""
    schedule = latency_log.op_schedule(args.target_rate, args.batch_size, origin_s)
    reporter = ThroughputReporter("scylladb load")
    for batch in batched(read_corpus(args.corpus, args.max_docs), args.batch_size):
        send = batch_sender(args, session, statement, batch, tally)
        op = next(schedule)
        latency_log.timed_op(log, op.i, op.t_intended_s, "insert", len(batch), send)
        reporter.add(len(batch))
    reporter.finish()


def run_load(args: argparse.Namespace,
             session) -> tuple[latency_log.LatencyLog, load_retry.RetryTally]:
    statement = prepare_insert(session, args.table)
    header = load_header(args, session)
    origin_s = time.perf_counter()
    tally = load_retry.RetryTally()
    with latency_log.open_log(args.latency_log, header, origin_s) as log:
        dispatch_all(args, session, statement, log, origin_s, tally)
    return log, tally


def main() -> int:
    args = parse_args()
    cluster, session = connect(args.hosts.split(","), args.port, args.keyspace)
    try:
        log, tally = run_load(args, session)
    finally:
        cluster.shutdown()
    print(f"scylladb load: {log.summary_line()}", file=sys.stderr)
    summary = log.summary()
    print(f"loaded {summary['docs']} docs into {args.keyspace}.{args.table} "
          f"({tally.line()})", file=sys.stderr)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
