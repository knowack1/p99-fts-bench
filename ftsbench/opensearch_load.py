"""Bulk-load the canonical corpus into OpenSearch via the raw _bulk API.

Raw NDJSON on purpose — this script documents exactly what the ingest path
looks like on the wire.

Everything about *how* the client offers work — the schedule, the in-flight
bound, the timing, the retry accounting — lives in `ftsbench.load_driver` and is
shared with `scylla_load`. This module supplies only the two engine-specific
halves: build one `_bulk` payload, and POST it. See that module for why the two
loaders had to be unified.

Usage: python3 -m ftsbench.opensearch_load --corpus data/corpus.jsonl \
           --batch-size 500 --concurrency 16
"""
import argparse
import json
import sys
import threading
from functools import partial
import requests

from . import load_driver, load_retry, samplers

DEFAULT_URL = "http://localhost:9200"
DEFAULT_INDEX = "wiki-articles"
BULK_TIMEOUT_S = 120
SETTINGS_TIMEOUT_S = 30
RESTORED_REFRESH_INTERVAL = "1s"

_thread_local = threading.local()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    load_driver.add_common_args(parser)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
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
    would silently re-serialise the concurrency the driver exists to add."""
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


def send(url: str, payload: bytes, tally: load_retry.RetryTally) -> None:
    """The engine-specific half. Runs on a worker thread."""
    load_retry.send_with_retries([payload], partial(attempt_bulk, thread_session(), url),
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


def build_loader(args: argparse.Namespace, url: str) -> load_driver.EngineLoader:
    return load_driver.EngineLoader(
        name="opensearch", engine="opensearch", op_kind="bulk",
        engine_version=samplers.OpenSearchSampler(url, args.index).version(),
        encode=partial(bulk_payload, index=args.index),
        send=partial(send, url),
        header_fields={
            "index": args.index,
            "refresh_during_load": not args.no_refresh_during_load,
        },
    )


def run_load(args: argparse.Namespace, url: str):
    return load_driver.run(args, build_loader(args, url))


def load_with_refresh_restored(args: argparse.Namespace, url: str,
                               control: requests.Session):
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
    load_driver.warn_if_client_bound(args.concurrency)
    control = requests.Session()
    log, tally = load_with_refresh_restored(args, url, control)
    print(f"opensearch load: {log.summary_line()}", file=sys.stderr)
    count = refresh_and_count(control, url, args.index)
    print(f"index '{args.index}' now holds {count} docs ({tally.line()})",
          file=sys.stderr)
    return 1 if log.summary()["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
