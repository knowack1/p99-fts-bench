"""Bulk-load the canonical corpus into OpenSearch via the raw _bulk API.

Raw NDJSON on purpose — this script documents exactly what the ingest path
looks like on the wire. The rate control and in-flight concurrency an earlier
version of this docstring deferred to opensearch-py's `helpers.parallel_bulk`
are implemented here instead (`--target-rate`, `--concurrency`), because the
benchmark has to record per-operation timings against a fixed schedule and a
helper that owns its own dispatch loop cannot report `t_intended_s`.

Concurrency is a fairness fix, not a speed knob. Measured serially at
`MAX_DOCS=50000`, OpenSearch's `write` thread pool sat at 0.50-0.77 active of 3
with a queue of 0.00 and throughput rose 27% from batch size alone: the
published 8,962 docs/s was the client's ceiling, not the engine's. `scylla_load`
has driven 128-way concurrency since the start, so until both sides are set to a
stated in-flight depth the two ingest numbers are not comparable at all. See
`PROGRESS.md` and `TUNING.md`.

Usage: python3 -m ftsbench.opensearch_load --corpus data/corpus.jsonl
"""
import argparse
import json
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from concurrent import futures
from typing import Any

import requests

from . import latency_log, load_retry, pacer, runmeta, samplers
from .corpus import batched, read_corpus
from .progress import ThroughputReporter

DEFAULT_URL = "http://localhost:9200"
DEFAULT_INDEX = "wiki-articles"
DEFAULT_BATCH_DOCS = 500
DEFAULT_CONCURRENCY = 1
BULK_TIMEOUT_S = 120
SETTINGS_TIMEOUT_S = 30
RESTORED_REFRESH_INTERVAL = "1s"

_thread_local = threading.local()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_DOCS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help="in-flight _bulk requests (1 = serial, which is "
                             "client-bound and not comparable with scylla_load)")
    parser.add_argument("--target-rate", type=float, default=0.0,
                        help="offered ingest rate in docs/s; 0 = unpaced "
                             "closed-loop dispatch, where latency_ms equals "
                             "service_ms and is not an SLA latency")
    parser.add_argument("--latency-log", default=None,
                        help="write one latency_op record per _bulk (chart C3)")
    parser.add_argument("--label", default="", help="free-form run label recorded in the header")
    parser.add_argument("--cache-state", default="unspecified",
                        help="cold|warm|unspecified — recorded for the chart footer")
    parser.add_argument("--max-docs", type=int, default=0, help="0 = no cap")
    parser.add_argument("--no-refresh-during-load", action="store_true",
                        help="set refresh_interval=-1 while loading, restore after "
                             "(a build-throughput tuning knob — publish it if used)")
    return parser.parse_args()


def bulk_payload(batch: list[dict], index: str) -> bytes:
    lines = []
    for doc in batch:
        lines.append(json.dumps({"index": {"_index": index, "_id": str(doc["id"])}}))
        lines.append(json.dumps(
            {"page_id": doc["id"], "title": doc["title"], "body": doc["text"]},
            ensure_ascii=False,
        ))
    return ("\n".join(lines) + "\n").encode("utf-8")


def first_bulk_error(body: dict) -> object:
    for item in body.get("items", []):
        error = item.get("index", {}).get("error")
        if error:
            return error
    return "unknown"


def thread_session() -> requests.Session:
    """One Session per worker thread. `requests.Session` is not documented
    thread-safe, and a shared one caps out at its connection-pool size, which
    would silently re-serialise the concurrency this exists to add."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def send_bulk(session: requests.Session, url: str, payload: bytes) -> None:
    response = session.post(
        f"{url}/_bulk",
        data=payload,
        headers={"Content-Type": "application/x-ndjson"},
        timeout=BULK_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"bulk request had item failures, first: {first_bulk_error(body)}")


def attempt_bulk(session: requests.Session, url: str,
                 payloads: list[bytes]) -> load_retry.Attempt:
    """One _bulk request is one retryable item. Resending it cannot duplicate
    anything: bulk_payload names every document's _id, so a repeat overwrites."""
    try:
        send_bulk(session, url, payloads[0])
    except Exception as error:
        return load_retry.Attempt(list(payloads), f"{type(error).__name__}: {error}")
    return load_retry.Attempt([])


def send_bulk_with_retries(session: requests.Session, url: str, payload: bytes,
                           tally: load_retry.RetryTally) -> None:
    """Same policy as scylla_load, for the reason given in ftsbench.load_retry:
    the two ingest paths must differ in what the engines do, not in how hard the
    client tries."""
    load_retry.send_with_retries([payload], partial(attempt_bulk, session, url),
                                 tally)


def set_refresh_interval(session: requests.Session, url: str, index: str, interval: str) -> None:
    response = session.put(
        f"{url}/{index}/_settings",
        json={"index": {"refresh_interval": interval}},
        timeout=SETTINGS_TIMEOUT_S,
    )
    response.raise_for_status()


def refresh_and_count(session: requests.Session, url: str, index: str) -> int:
    session.post(f"{url}/{index}/_refresh", timeout=BULK_TIMEOUT_S).raise_for_status()
    response = session.get(f"{url}/{index}/_count", timeout=SETTINGS_TIMEOUT_S)
    response.raise_for_status()
    return response.json()["count"]


def _surface_errors(done: set) -> None:
    """A worker exception is a harness bug: an operation the engine rejected is
    recorded by `timed_op` and never raises, so anything arriving here must not
    be absorbed into a plausible-looking series."""
    for future in done:
        future.result()


class BulkPool:
    """Bounded set of in-flight `_bulk` requests.

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


def bulk_work(log: latency_log.LatencyLog, op: pacer.Op, url: str,
              payload: bytes, n_docs: int,
              tally: load_retry.RetryTally) -> Callable[[], None]:
    """Each worker times its own request, against the run's shared origin."""
    def work() -> None:
        latency_log.timed_op(
            log, op.i, op.t_intended_s, "bulk", n_docs,
            lambda: send_bulk_with_retries(thread_session(), url, payload, tally))
    return work


def load_header(args: argparse.Namespace, url: str) -> dict[str, Any]:
    return runmeta.header(
        producer="opensearch_load", engine="opensearch",
        engine_version=samplers.OpenSearchSampler(url, args.index).version(),
        label=args.label, cache_state=args.cache_state, corpus=args.corpus,
        max_docs=args.max_docs, index=args.index, batch_size=args.batch_size,
        concurrency=args.concurrency, target_rate_docs_per_s=args.target_rate,
        refresh_during_load=not args.no_refresh_during_load,
        retry_attempts=load_retry.DEFAULT_POLICY.attempts,
    )


def warn_if_client_bound(concurrency: int) -> None:
    if concurrency <= 1:
        print("WARNING: --concurrency 1 sends one _bulk at a time; the measured "
              "rate is the client's, not the engine's. Not comparable with "
              "scylla_load and not quotable. See TUNING.md.", file=sys.stderr)


def dispatch_all(pool: BulkPool, log: latency_log.LatencyLog,
                 args: argparse.Namespace, url: str, origin_s: float,
                 tally: load_retry.RetryTally) -> None:
    """Documents are read and batched on this thread alone, so batch k holds the
    same documents at every concurrency; workers only send what they are given.
    Payload encoding also stays here, which keeps `service_ms` HTTP-only."""
    schedule = latency_log.op_schedule(args.target_rate, args.batch_size, origin_s)
    reporter = ThroughputReporter("opensearch load")
    for batch in batched(read_corpus(args.corpus, args.max_docs), args.batch_size):
        payload = bulk_payload(batch, args.index)
        op = next(schedule)
        pool.submit(bulk_work(log, op, url, payload, len(batch), tally))
        reporter.add(len(batch))
    pool.drain()
    reporter.finish()


def run_load(args: argparse.Namespace,
             url: str) -> tuple[latency_log.LatencyLog, load_retry.RetryTally]:
    header = load_header(args, url)
    origin_s = time.perf_counter()
    tally = load_retry.RetryTally()
    with latency_log.open_log(args.latency_log, header, origin_s) as log, \
            futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        dispatch_all(BulkPool(executor, args.concurrency), log, args, url,
                     origin_s, tally)
    return log, tally


def load_with_refresh_restored(
        args: argparse.Namespace, url: str, control: requests.Session
) -> tuple[latency_log.LatencyLog, load_retry.RetryTally]:
    if args.no_refresh_during_load:
        set_refresh_interval(control, url, args.index, "-1")
    try:
        return run_load(args, url)
    finally:
        if args.no_refresh_during_load:
            set_refresh_interval(control, url, args.index, RESTORED_REFRESH_INTERVAL)


def main() -> int:
    args = parse_args()
    url = args.url.rstrip("/")
    warn_if_client_bound(args.concurrency)
    control = requests.Session()
    log, tally = load_with_refresh_restored(args, url, control)
    print(f"opensearch load: {log.summary_line()}", file=sys.stderr)
    count = refresh_and_count(control, url, args.index)
    print(f"index '{args.index}' now holds {count} docs ({tally.line()})",
          file=sys.stderr)
    return 1 if log.summary()["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
