"""Steady add/delete churn against a resident index (deck S28).

Emits a paced stream of alternating ADD and DELETE operations so that after the
warm-in the index size is constant to within the ring size and the engine is
doing real index-and-forget work at a known rate. The query side of S28 runs
concurrently as ordinary `cell_bench` cells; this module only produces the
churn and reports whether it kept up.

    python3 -m ftsbench.churn_load --opensearch-disk-store-refresh3 \\
        --url http://sut:9200 --corpus data/corpus.jsonl \\
        --rate 2000 --duration 180 --ring 20000 \\
        --batch-size 200 --concurrency 16 \\
        --output data/churn/churn-opensearch-2000-r1.jsonl

**Everything about how the client offers work now comes from
`ftsbench.load_driver`, shared with the two build loaders.** It did not, and
that cost a published result. This module held its own dispatch per engine:
OpenSearch pipelined through a `ThreadPoolExecutor` with a hardcoded
`IN_FLIGHT = 4`, ignoring `--concurrency` entirely, while ScyllaDB honoured
`--concurrency` and additionally serialised its insert phase against its delete
phase. So one flag named two different quantities, and each engine's churn
ceiling was a property of its own constant: 4 bulks x 200 items / ~125 ms
≈ 6,400 ops/s for OpenSearch, 16 rows / ~2.0 ms ≈ 7,900 ops/s for ScyllaDB.
The 8,000 ops/s row of the S28 grid then reported whichever constant fell
below 8,000 as an engine that "could not sustain" the rate. It measured two
client constants. See `load_driver`'s docstring for the first time this
happened.

`--rate` is TOTAL operations/s (adds and deletes each counted as one), matching
the S28 x-axis, and it drives the driver's own pacer: rate / batch-size gives
the operation rate, so the schedule is the same open-loop absolute schedule the
C3 ingest runs use rather than a third hand-rolled pacing loop.

One churn operation is one request carrying `--batch-size` items, adds and
deletes mixed — one `_bulk` on the OpenSearch side, one `execute_concurrent` of
prepared INSERT/DELETE statements on the ScyllaDB side. `n_docs` therefore
counts churn items, not documents written; the header says so.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import uuid
from functools import partial
from typing import Iterator

from . import (churn_stream, load_driver, load_retry, opensearch_load, runmeta,
               scylla_load, target)
from .corpus import read_corpus

DEFAULT_URL = "http://localhost:9200"
DEFAULT_INDEX = "wiki-articles"
DEFAULT_HOSTS = "127.0.0.1"
DEFAULT_PORT = 9042
DEFAULT_KEYSPACE = "wiki"
DEFAULT_SAMPLE_DOCS = 20000
DEFAULT_RING = 20000
GATE_FRACTION = 0.95
# Churn items carry no page_id of their own; the column exists in the schema and
# in the OpenSearch mapping, so both sides write the same placeholder and the
# wire shape stays byte-identical to every S28 artifact already on disk.
CHURN_PAGE_ID = 0


def parse_args() -> argparse.Namespace:
    return parse_args_from(None)


def parse_args_from(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    load_driver.add_common_args(parser)
    target.add_target_args(parser, engines=("opensearch", "scylladb"))
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--hosts", default=DEFAULT_HOSTS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keyspace", default=DEFAULT_KEYSPACE)
    parser.add_argument("--sample-docs", type=int, default=DEFAULT_SAMPLE_DOCS,
                        help="corpus prefix reused round-robin as ADD bodies")
    parser.add_argument("--rate", type=float, required=True,
                        help="total churn ops/s (adds + deletes)")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--ring", type=int, default=DEFAULT_RING,
                        help="adds outstanding before deletes begin")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def corpus_sample(path: str, count: int) -> list[dict]:
    """A corpus prefix, reused round-robin as ADD bodies.

    Real bodies rather than synthetic text: the analyzer, the term
    distribution and the segment sizes all follow from what is indexed, and
    churn built from generated filler would exercise none of them.
    """
    documents = [{"title": raw.get("title", ""),
                  "body": raw.get("text") or raw.get("body", "")}
                 for raw in read_corpus(path, count)]
    if not documents:
        raise SystemExit(f"no documents in {path}")
    return documents


def bulk_payload(items: list[churn_stream.ChurnItem], index: str) -> bytes:
    lines: list[str] = []
    for item in items:
        lines += bulk_lines(item, index)
    return ("\n".join(lines) + "\n").encode("utf-8")


def bulk_lines(item: churn_stream.ChurnItem, index: str) -> list[str]:
    if item.kind == churn_stream.DELETE:
        return [json.dumps({"delete": {"_index": index, "_id": item.doc_id}})]
    return [
        json.dumps({"index": {"_index": index, "_id": item.doc_id}}),
        json.dumps({"page_id": CHURN_PAGE_ID,
                    "title": item.document["title"],
                    "body": item.document["body"]}, ensure_ascii=False),
    ]


def statement_parameters(items: list[churn_stream.ChurnItem], insert,
                         delete) -> list[tuple]:
    return [(delete, (uuid.UUID(item.doc_id),))
            if item.kind == churn_stream.DELETE
            else (insert, (uuid.UUID(item.doc_id), CHURN_PAGE_ID,
                           item.document["title"], item.document["body"]))
            for item in items]


def send_statements(attempt, statements: list[tuple],
                    tally: load_retry.RetryTally) -> None:
    """The engine-specific half, on a worker thread. The driver's Session is
    thread-safe, unlike `requests.Session` on the OpenSearch side."""
    load_retry.send_with_retries(statements, attempt, tally)


def churn_header_fields(args: argparse.Namespace) -> dict:
    return {
        "churn_ops_per_s": args.rate,
        "duration_s": args.duration,
        "ring": args.ring,
        "sample_docs": args.sample_docs,
        # `n_docs` is the shared latency_op field name; for churn it counts
        # items, and a DELETE is not a document written. Disclosed rather than
        # renamed, because renaming reaches C3, C5, C6 and every artifact.
        "n_docs_unit": "churn items (adds + deletes)",
        **target.header_fields(target.resolve(args)),
    }


@contextlib.contextmanager
def opensearch_loader(args: argparse.Namespace) -> Iterator[load_driver.EngineLoader]:
    url = args.url.rstrip("/")
    yield load_driver.EngineLoader(
        name="churn", engine="opensearch", op_kind=churn_stream.OP_STEADY,
        engine_version="unknown",
        encode=partial(bulk_payload, index=args.index),
        send=partial(opensearch_load.send, url),
        header_fields={"index": args.index, **churn_header_fields(args)},
    )


@contextlib.contextmanager
def scylla_loader(args: argparse.Namespace) -> Iterator[load_driver.EngineLoader]:
    cluster, session = scylla_load.connect(args.hosts.split(","), args.port,
                                          args.keyspace)
    try:
        insert = session.prepare(
            "INSERT INTO articles (article_id, page_id, title, body) "
            "VALUES (?, ?, ?, ?)")
        delete = session.prepare("DELETE FROM articles WHERE article_id = ?")
        attempt = partial(scylla_load.attempt_statements, session, 0)
        yield load_driver.EngineLoader(
            name="churn", engine="scylladb", op_kind=churn_stream.OP_STEADY,
            engine_version=scylla_load.engine_version(session),
            encode=partial(statement_parameters, insert=insert, delete=delete),
            send=partial(send_statements, attempt),
            header_fields={"keyspace": args.keyspace,
                           **churn_header_fields(args)},
        )
    finally:
        cluster.shutdown()


def churn_loader(args: argparse.Namespace):
    if args.engine == "opensearch":
        return opensearch_loader(args)
    return scylla_loader(args)


def churn_summary(args: argparse.Namespace, log, tally,
                  stream: churn_stream.ChurnStream, wall_s: float) -> dict:
    """Derived wholly from the driver's own accounting, never hand-counted.

    `ok_docs` is accumulated on the worker thread that ran the operation, inside
    `latency_log.timed_op`, and reached only after the send has RETURNED. There
    is no per-engine submission path left for anything to be counted on — which
    is the structural reason the achieved rate now means the same thing on both
    engines, rather than a convention someone has to keep.
    """
    summary = log.summary()
    retries = tally.summary()
    landed = summary["ok_docs"]
    return {
        "record": "churn_summary",
        "offered_ops_per_s": args.rate,
        "achieved_ops_per_s": round(landed / wall_s, 2) if wall_s else 0.0,
        "ops_sent": landed,
        "requests": summary["ops"],
        # Failed REQUESTS, which is what every other artifact means by `errors`.
        # The previous implementation counted items in the loop and requests in
        # the drain, so one failure was worth 200 on ScyllaDB and 1 on
        # OpenSearch.
        "errors": summary["errors"],
        "failed_items": summary["docs"] - landed,
        "retries": retries["retries"],
        "retried_items": retries["retried_items"],
        "first_error": summary["first_error"],
        "adds": stream.adds,
        "deletes": stream.deletes,
        "ring_outstanding": stream.ring_outstanding,
        "wall_s": round(wall_s, 3),
        # The two numbers that say whether the churn writer itself was the
        # bottleneck. S28's stated caveat leaned on the query cells' queue_p99
        # for this; the churn side never had one.
        "p50_ms": summary.get("p50_ms"),
        "p99_ms": summary.get("p99_ms"),
        "queue_p99_ms": summary.get("queue_p99_ms"),
    }


def run_churn(args: argparse.Namespace) -> dict:
    documents = corpus_sample(args.corpus, args.sample_docs)
    stream = churn_stream.ChurnStream(documents, args.ring, args.batch_size)
    stopper = runmeta.Stopper()
    source = churn_stream.churn_source(stream, args.duration,
                                       lambda: stopper.stop)
    with churn_loader(args) as loader:
        log, tally, wall_s = load_driver.run_timed(args, loader, source)
    return churn_summary(args, log, tally, stream, wall_s)


def kept_up(args: argparse.Namespace, summary: dict) -> bool:
    return (summary["achieved_ops_per_s"] >= GATE_FRACTION * args.rate
            and summary["errors"] == 0)


def main() -> int:
    args = parse_args()
    target.resolve_into(args)
    # The driver paces on documents/s over batch_size; churn's rate is items/s
    # over the same batch, so they are the same quantity and the driver's
    # schedule replaces the hand-rolled pacing loop this module used to carry.
    args.target_rate = args.rate
    # Per-operation records go into the same artifact. Nothing consumed them
    # before, which is why "how does bulk service time scale with in-flight
    # depth" could not be answered from any churn file on disk.
    args.latency_log = args.output
    load_driver.warn_if_client_bound(args.concurrency)
    summary = run_churn(args)
    load_driver.append_record(args.output, summary)
    print(f"churn {args.rate:g} ops/s: "
          f"achieved={summary['achieved_ops_per_s']} "
          f"errors={summary['errors']}", file=sys.stderr)
    return 0 if kept_up(args, summary) else 1


if __name__ == "__main__":
    sys.exit(main())
