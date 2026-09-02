"""Steady add/delete churn against a resident index (deck S28).

Emits an open-loop, paced stream of alternating ADD (a fresh synthetic id
carrying a real corpus body) and DELETE (the oldest id this process added),
so after the warm-in the index size is constant to within the ring size and
the engine is doing real index-and-forget work at a known rate. The query
side of S28 runs concurrently as ordinary `cell_bench` cells; this module
only produces the churn and reports whether it kept up.

    python3 -m ftsbench.churn_load --engine opensearch --url http://sut:9200 \\
        --corpus data/corpus.jsonl --rate 2000 --duration 180 \\
        --ring 20000 --output data/churn/churn-opensearch-2000-r1.jsonl

Rate accounting: --rate is TOTAL operations/s (adds + deletes together, each
counted as one), matching the S28 x-axis "churn rate". A tick that falls
behind is not skipped: the pacer is open-loop, and the summary reports
achieved vs offered so a churn-bound cell can be disqualified by the driver
rather than silently measured at a lower churn than its label claims.

Both sides do their write-path work exactly as in the build measurements:
OpenSearch gets `_bulk` index/delete actions under refresh_interval=3s;
ScyllaDB gets prepared INSERT/DELETE rows whose CDC feed the vector-store
tails under the 3 s threshold-free commit. The asymmetry disclosures from
COMPARABILITY.md carry over unchanged.
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import signal
import sys
import time
import uuid

import requests

from . import runmeta

BULK_OPS = 200
STOP = False


def _handle_term(signum, frame) -> None:
    """The grid driver stops the stream with SIGTERM once its row's query
    cells are done; the summary must still be written or the row gate reads
    an empty artifact and fails a healthy row."""
    global STOP
    STOP = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True,
                        choices=["opensearch", "scylladb"])
    parser.add_argument("--url", default="http://localhost:9200")
    parser.add_argument("--index", default="wiki-articles")
    parser.add_argument("--hosts", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9042)
    parser.add_argument("--keyspace", default="wiki")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--sample-docs", type=int, default=20000,
                        help="corpus prefix reused round-robin as ADD bodies")
    parser.add_argument("--rate", type=float, required=True,
                        help="total churn ops/s (adds + deletes)")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--ring", type=int, default=20000,
                        help="adds outstanding before deletes begin")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", default="")
    return parser.parse_args()


def corpus_sample(path: str, count: int) -> list[dict]:
    docs = []
    with open(path, encoding="utf-8") as fh:
        for line in itertools.islice(fh, count):
            raw = json.loads(line)
            docs.append({"title": raw.get("title", ""),
                         "body": raw.get("text") or raw.get("body", "")})
    if not docs:
        raise SystemExit(f"no documents in {path}")
    return docs


RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 0.05


def with_retries(attempt) -> int:
    """Run `attempt` up to RETRY_ATTEMPTS times; returns how many retries it
    took. Same policy as ftsbench.load_retry on the write path: a transient
    client-side ConnectionBusy must not fail a row (it cost the first laptop
    campaign two repetitions), and a retry that exhausts its budget still
    raises — silently absorbing every error would turn a failing engine into
    a healthy churn stream."""
    for retry in range(RETRY_ATTEMPTS):
        try:
            attempt()
            return retry
        except Exception:  # noqa: BLE001 — re-raised on exhaustion
            if retry == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BACKOFF_S * (2 ** retry))
    return RETRY_ATTEMPTS


class OpenSearchChurn:
    def __init__(self, args: argparse.Namespace):
        self.url = args.url.rstrip("/")
        self.index = args.index
        self.session = requests.Session()

    def apply(self, adds: list[tuple[str, dict]], deletes: list[str]) -> None:
        lines = []
        for doc_id, doc in adds:
            lines.append(json.dumps({"index": {"_index": self.index,
                                               "_id": doc_id}}))
            lines.append(json.dumps({"page_id": 0, "title": doc["title"],
                                     "body": doc["body"]}))
        for doc_id in deletes:
            lines.append(json.dumps({"delete": {"_index": self.index,
                                                "_id": doc_id}}))
        payload = ("\n".join(lines) + "\n").encode()
        response = self.session.post(
            f"{self.url}/_bulk", data=payload,
            headers={"Content-Type": "application/x-ndjson"}, timeout=30)
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            failed = [item for item in body["items"]
                      for op in item.values()
                      if op.get("status", 200) >= 300
                      and op.get("status") != 404]
            if failed:
                raise RuntimeError(f"bulk failures: {failed[0]}")


class ScyllaChurn:
    def __init__(self, args: argparse.Namespace):
        from cassandra.cluster import Cluster
        from cassandra.concurrent import execute_concurrent_with_args
        self._execute_concurrent = execute_concurrent_with_args
        self.cluster = Cluster(args.hosts.split(","), port=args.port)
        self.session = self.cluster.connect(args.keyspace)
        self.insert = self.session.prepare(
            "INSERT INTO articles (article_id, page_id, title, body) "
            "VALUES (?, ?, ?, ?)")
        self.delete = self.session.prepare(
            "DELETE FROM articles WHERE article_id = ?")
        self.concurrency = args.concurrency

    def apply(self, adds: list[tuple[str, dict]], deletes: list[str]) -> None:
        insert_rows = [(uuid.UUID(doc_id), 0, doc["title"], doc["body"])
                       for doc_id, doc in adds]
        delete_rows = [(uuid.UUID(doc_id),) for doc_id in deletes]
        for statement, rows in ((self.insert, insert_rows),
                                (self.delete, delete_rows)):
            if not rows:
                continue
            results = self._execute_concurrent(
                self.session, statement, rows,
                concurrency=self.concurrency, raise_on_first_error=True)
            collections.deque(results, maxlen=0)


def build_churn(args: argparse.Namespace):
    return (OpenSearchChurn(args) if args.engine == "opensearch"
            else ScyllaChurn(args))


def churn_header(args: argparse.Namespace) -> dict:
    return runmeta.header(
        producer="churn_load", engine=args.engine, label=args.label,
        cache_state="warm", corpus=args.corpus, rate_ops_per_s=args.rate,
        duration_s=args.duration, ring=args.ring,
        sample_docs=args.sample_docs)


def run_churn(args: argparse.Namespace, engine, docs: list[dict]) -> dict:
    ring: collections.deque[str] = collections.deque()
    doc_cycle = itertools.cycle(docs)
    origin = time.perf_counter()
    sent = errors = retries = 0
    first_error = None
    next_i = 0
    while True:
        elapsed = time.perf_counter() - origin
        if STOP or elapsed >= args.duration:
            break
        target_ops = min(args.rate * elapsed + BULK_OPS, args.rate * args.duration)
        if sent >= target_ops:
            time.sleep(min(BULK_OPS / max(args.rate, 1), 0.25))
            continue
        batch = min(BULK_OPS, int(target_ops) - sent)
        adds, deletes = [], []
        while len(adds) + len(deletes) < batch:
            if len(ring) >= args.ring:
                deletes.append(ring.popleft())
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"churn-{next_i}"))
            next_i += 1
            adds.append((doc_id, next(doc_cycle)))
            ring.append(doc_id)
        try:
            retries += with_retries(lambda: engine.apply(adds, deletes))
            sent += len(adds) + len(deletes)
        except Exception as err:  # noqa: BLE001 — counted, reported, gated
            errors += len(adds) + len(deletes)
            if first_error is None:
                first_error = str(err)[:300]
    wall = time.perf_counter() - origin
    return {
        "record": "churn_summary",
        "offered_ops_per_s": args.rate,
        "achieved_ops_per_s": round(sent / wall, 2) if wall else 0.0,
        "ops_sent": sent, "errors": errors, "retries": retries,
        "first_error": first_error,
        "ring_outstanding": len(ring), "wall_s": round(wall, 3),
    }


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_term)
    args = parse_args()
    docs = corpus_sample(args.corpus, args.sample_docs)
    engine = build_churn(args)
    with open(args.output, "w", encoding="utf-8") as out:
        runmeta.write_record(out, churn_header(args))
        summary = run_churn(args, engine, docs)
        runmeta.write_record(out, summary)
    achieved = summary["achieved_ops_per_s"]
    print(f"churn {args.rate:g} ops/s: achieved={achieved} "
          f"errors={summary['errors']}", file=sys.stderr)
    kept_up = achieved >= 0.95 * args.rate and summary["errors"] == 0
    return 0 if kept_up else 1


if __name__ == "__main__":
    sys.exit(main())
