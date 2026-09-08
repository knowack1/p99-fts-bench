"""Load the canonical corpus into ScyllaDB with prepared, concurrent INSERTs.

Idempotent: article_id is the corpus's deterministic uuid5 of the page id, so
re-running overwrites the same rows. Apply scylladb/schema.cql first; create
the fulltext index before or after loading depending on which ingest path
(CDC tail vs. bootstrap scan) the run is meant to exercise.

Everything about *how* the client offers work — the schedule, the in-flight
bound, the timing, the retry accounting — lives in `ftsbench.load_driver` and is
shared with `opensearch_load`. This module supplies only the two
engine-specific halves: turn a batch into bound parameters, and execute them.

One operation is one batch of `--batch-size` rows, and `--concurrency` batches
are in flight — the same quantities `opensearch_load` uses, where one operation
is one `_bulk` of `--batch-size` documents. Until this was unified the two
flags named different things and the ScyllaDB side encoded everything on a
single thread, which capped it near 9.8k docs/s and was mistaken for an engine
ceiling. See `load_driver` and `TUNING.md`.

Usage: python3 -m ftsbench.scylla_load --corpus data/corpus.jsonl \
           --hosts 127.0.0.1 --batch-size 500 --concurrency 16
"""
import argparse
import sys
import uuid
from functools import partial

from cassandra.cluster import Session

from . import load_driver, load_retry

DEFAULT_HOSTS = "127.0.0.1"
DEFAULT_PORT = 9042
DEFAULT_KEYSPACE = "wiki"
DEFAULT_TABLE = "articles"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    load_driver.add_common_args(parser)
    parser.add_argument("--hosts", default=DEFAULT_HOSTS,
                        help="comma-separated contact points")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keyspace", default=DEFAULT_KEYSPACE)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--rows-in-flight", type=int, default=0,
                        help="driver-internal rows in flight within one batch; "
                             "0 = the whole batch, which mirrors one _bulk "
                             "carrying --batch-size documents. Not a fairness "
                             "knob — --concurrency is the shared one")
    parser.add_argument("--unlogged-batch-rows", type=int, default=0,
                        help="DIAGNOSTIC: group this many rows per UNLOGGED "
                             "BATCH (0 = per-row prepared statements, the "
                             "default and the only mode fit for a published "
                             "write number). See attempt_unlogged_batches.")
    return parser.parse_args()


def connect(hosts: list[str], port: int, keyspace: str):
    from cassandra.cluster import Cluster

    cluster = Cluster(hosts, port=port)
    return cluster, cluster.connect(keyspace)


def engine_version(session: Session) -> str:
    try:
        return str(session.execute(
            "SELECT release_version FROM system.local").one().release_version)
    except Exception:
        return "unknown"


def prepare_insert(session: Session, table: str):
    return session.prepare(
        f"INSERT INTO {table} (article_id, page_id, title, body) VALUES (?, ?, ?, ?)"
    )


def insert_parameters(batch: list[dict]) -> list[tuple]:
    return [
        (uuid.UUID(doc["uuid"]), doc["id"], doc["title"], doc["text"])
        for doc in batch
    ]


def concurrent_results(session: Session, statement, parameters: list[tuple],
                       rows_in_flight: int) -> list:
    """`raise_on_first_error=False` so every row is attempted and the per-row
    outcomes come back. Raising on the first one abandoned the rest of the batch
    unsent, which is how a single ConnectionBusy cost one repetition 380
    documents — see ftsbench.load_retry."""
    from cassandra.concurrent import execute_concurrent_with_args

    return execute_concurrent_with_args(
        session, statement, parameters,
        concurrency=rows_in_flight or len(parameters),
        raise_on_first_error=False,
    )


def attempt_rows(session: Session, statement, rows_in_flight: int,
                 parameters: list[tuple]) -> load_retry.Attempt:
    """Driver results are (success, result-or-exception) in the order sent."""
    results = concurrent_results(session, statement, parameters, rows_in_flight)
    failed = [row for row, outcome in zip(parameters, results) if not outcome[0]]
    error = next((f"{type(outcome[1]).__name__}: {outcome[1]}"
                  for outcome in results if not outcome[0]), "")
    return load_retry.Attempt(failed, error)


def attempt_unlogged_batches(session: Session, statement, batch_rows: int,
                             rows_in_flight: int,
                             parameters: list[tuple]) -> load_retry.Attempt:
    """UNLOGGED BATCH, in groups of `batch_rows`.

    Diagnostic mode only. Every row here is its own partition, so a multi-row
    batch is the documented anti-pattern: the coordinator fans out to every
    partition's replica set and shard-aware routing is defeated. ScyllaDB logs a
    warning per batch and returns WriteTimeout if pushed too hard.

    It was added because the per-row path was bound by this client's GIL when
    the whole loader ran on one thread. `load_driver` removed that constraint,
    so this exists now only to reproduce older runs — never to produce a
    published write number.
    """
    from cassandra.query import BatchStatement, BatchType

    pending, failed, error = [], [], ""
    for start in range(0, len(parameters), batch_rows):
        rows = parameters[start:start + batch_rows]
        batch = BatchStatement(batch_type=BatchType.UNLOGGED)
        for row in rows:
            batch.add(statement, row)
        pending.append((session.execute_async(batch), rows))
        if len(pending) >= (rows_in_flight or len(parameters)):
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


def batch_attempt(args: argparse.Namespace, session: Session, statement):
    if args.unlogged_batch_rows:
        return partial(attempt_unlogged_batches, session, statement,
                       args.unlogged_batch_rows, args.rows_in_flight)
    return partial(attempt_rows, session, statement, args.rows_in_flight)


def send(attempt, parameters: list[tuple], tally: load_retry.RetryTally) -> None:
    """The engine-specific half. Runs on a worker thread; the driver's Session
    is thread-safe, unlike `requests.Session` on the OpenSearch side."""
    load_retry.send_with_retries(parameters, attempt, tally)


def build_loader(args: argparse.Namespace, session: Session,
                 statement) -> load_driver.EngineLoader:
    return load_driver.EngineLoader(
        name="scylla", engine="scylladb", op_kind="insert",
        engine_version=engine_version(session),
        encode=insert_parameters,
        send=partial(send, batch_attempt(args, session, statement)),
        header_fields={
            "keyspace": args.keyspace,
            "table": args.table,
            "rows_in_flight": args.rows_in_flight or args.batch_size,
            "unlogged_batch_rows": args.unlogged_batch_rows,
        },
    )


def run_load(args: argparse.Namespace, session: Session):
    statement = prepare_insert(session, args.table)
    return load_driver.run(args, build_loader(args, session, statement))


def main() -> int:
    args = parse_args()
    load_driver.warn_if_client_bound(args.concurrency)
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
