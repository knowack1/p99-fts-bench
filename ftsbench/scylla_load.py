"""Load the canonical corpus into ScyllaDB with prepared, concurrent INSERTs.

Idempotent: article_id is the corpus's deterministic uuid5 of the page id, so
re-running overwrites the same rows. Apply scylladb/schema.cql first; create
the fulltext index before or after loading depending on which ingest path
(CDC tail vs. bootstrap scan) the run is meant to exercise.

Everything about *how* the client offers work — the schedule, the in-flight
bound, the timing, the retry accounting — lives in `ftsbench.load_driver` and is
shared with `opensearch_load`. This module supplies only the two
engine-specific halves: turn a document into bound parameters, and execute it.

**One operation is one prepared INSERT, and `--concurrency` is the only knob.**
There is no CQL wire batch: every row is its own statement, so a batch flag here
could only ever set a client-side dispatch window while reading like a wire
quantity — and a second in-flight bound beside `--concurrency` could only
disagree with it. Both are therefore absent rather than pinned to 1, and the
build-rate ceiling is found by raising `--concurrency` alone. The batch axis
belongs to `opensearch_load`, where a `_bulk` really does carry N documents;
what one request carries still differs between the engines, and that belongs in
the chart footer rather than in a knob. See `load_driver` and `TUNING.md`.

Usage: python3 -m ftsbench.scylla_load --corpus data/corpus.jsonl \
           --hosts 127.0.0.1 --concurrency 16
"""
import argparse
import asyncio
import sys
import uuid
from functools import partial

from cassandra.cluster import Session

from . import load_driver, load_retry, mp_load

DEFAULT_HOSTS = "127.0.0.1"
DEFAULT_PORT = 9042
DEFAULT_KEYSPACE = "wiki"
DEFAULT_TABLE = "articles"
# One row per operation, so --concurrency counts outstanding requests on both
# engines. Not a default a caller can raise: see the module docstring.
DOCS_PER_OPERATION = 1


def add_engine_args(parser: argparse.ArgumentParser) -> None:
    """The ScyllaDB-specific half, registered here so `mp_load`'s CLI can offer
    the same flags to a sharded run rather than growing its own copy."""
    parser.add_argument("--hosts", default=DEFAULT_HOSTS,
                        help="comma-separated contact points")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keyspace", default=DEFAULT_KEYSPACE)
    parser.add_argument("--table", default=DEFAULT_TABLE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    load_driver.add_common_args(parser, batch_size=False)
    add_engine_args(parser)
    mp_load.add_sharding_args(parser)
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


def awaitable(response_future) -> "asyncio.Future":
    """Bridge one driver `ResponseFuture` onto the dispatch loop.

    The driver's callbacks fire on ITS reactor thread, not ours, so the result
    has to cross with `call_soon_threadsafe`; setting it directly would mutate
    an asyncio Future from the wrong thread and lose or corrupt completions
    under load. This is the whole reason the CQL side can be async without a
    thread per statement: the reactor is already doing the multiplexing, and
    `execute_concurrent_with_args` was only ever a blocking wrapper around it.
    """
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()

    def settle(setter, value):
        if not waiter.done():
            setter(value)

    response_future.add_callbacks(
        lambda result: loop.call_soon_threadsafe(
            settle, waiter.set_result, result),
        lambda exc: loop.call_soon_threadsafe(
            settle, waiter.set_exception, exc),
    )
    return waiter


async def execute_all(session: Session, statements: list[tuple]) -> list:
    """Every item is attempted and the per-item outcomes come back, in the order
    sent. Abandoning the rest of an operation on the first failure is how a
    single ConnectionBusy cost one repetition 380 documents — see
    ftsbench.load_retry.

    Nothing is bounded here. The only in-flight bound is the driver's
    `--concurrency`, which holds N operations; a second bound inside one
    operation could only disagree with it.
    """
    async def one(statement, params):
        try:
            return (True, await awaitable(
                session.execute_async(statement, params)))
        except Exception as exc:
            return (False, exc)

    return await asyncio.gather(
        *(one(statement, params) for statement, params in statements))


def outcome_of(sent: list, results: list) -> load_retry.Attempt:
    """Driver results are (success, result-or-exception) in the order sent.

    Shared by the single-statement and mixed-statement paths so the two cannot
    disagree about what counts as a failed item.
    """
    failed = [item for item, outcome in zip(sent, results) if not outcome[0]]
    error = next((f"{type(outcome[1]).__name__}: {outcome[1]}"
                  for outcome in results if not outcome[0]), "")
    return load_retry.Attempt(failed, error)


async def attempt_rows(session: Session, statement,
                       parameters: list[tuple]) -> load_retry.Attempt:
    """One operation carries one row, so this is one INSERT — but it stays
    list-shaped because `load_retry` resends the failed items of an attempt."""
    results = await execute_all(
        session, [(statement, params) for params in parameters])
    return outcome_of(parameters, results)


async def attempt_statements(session: Session,
                             statements: list[tuple]) -> load_retry.Attempt:
    """One operation carrying a mix of statements.

    Churn mixes INSERT and DELETE in one operation, so each entry carries its
    own statement rather than cycling one statement over many parameter sets.
    Issued together rather than statement-kind by statement-kind, because the
    OpenSearch side puts adds and deletes in a single `_bulk`: doing it in two
    passes here would make one engine pay two round trips for the operation the
    other completes in one, and that gap would read as an engine difference.
    """
    results = await execute_all(session, statements)
    return outcome_of(statements, results)


async def send(attempt, parameters: list[tuple],
               tally: load_retry.RetryTally) -> None:
    """The engine-specific half. Runs on the dispatch loop: the driver's own
    reactor thread does the I/O, so an outstanding statement costs a callback
    rather than an OS thread."""
    await load_retry.send_with_retries_async(parameters, attempt, tally)


def build_loader(args: argparse.Namespace, session: Session,
                 statement) -> load_driver.EngineLoader:
    return load_driver.EngineLoader(
        name="scylla", engine="scylladb", op_kind="insert",
        engine_version=engine_version(session),
        docs_per_operation=DOCS_PER_OPERATION,
        encode=insert_parameters,
        send=partial(send, partial(attempt_rows, session, statement)),
        header_fields={
            "keyspace": args.keyspace,
            "table": args.table,
            **mp_load.shard_header_fields(args),
        },
    )


def run_load(args: argparse.Namespace, session: Session):
    statement = prepare_insert(session, args.table)
    return load_driver.run(args, build_loader(args, session, statement))


def report_load(args: argparse.Namespace, headline: str, docs: int,
                retries: str) -> None:
    print(f"scylladb load: {headline}", file=sys.stderr)
    print(f"loaded {docs} docs into {args.keyspace}.{args.table} "
          f"({retries})", file=sys.stderr)


def run_in_process(args: argparse.Namespace) -> int:
    cluster, session = connect(args.hosts.split(","), args.port, args.keyspace)
    try:
        log, tally = run_load(args, session)
    finally:
        cluster.shutdown()
    summary = log.summary()
    report_load(args, log.summary_line(), summary["docs"], tally.line())
    return 1 if summary["errors"] else 0


def run_workers(args: argparse.Namespace,
                shape: mp_load.ClientShape) -> int:
    """N worker processes, each with its own session over its own shard.

    Nothing connects in the parent: a session opened here would be inherited by
    no child — `spawn` carries no sockets — and its own reactor threads would
    compete with the pool for the client CPU this shape exists to spread.
    """
    summary = mp_load.run_sharded(args, "scylladb", shape)
    report_load(args, mp_load.summary_line(summary), summary["docs"],
                mp_load.retries_line(summary))
    return 1 if summary["errors"] else 0


def main() -> int:
    args = parse_args()
    load_driver.warn_if_client_bound(args.concurrency)
    shape = mp_load.client_shape(args)
    if shape is None:
        return run_in_process(args)
    return run_workers(args, shape)


if __name__ == "__main__":
    sys.exit(main())
