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
from collections.abc import Callable
from functools import partial
from urllib.parse import urlparse
import requests

from . import async_http, load_driver, load_retry, mp_load, samplers

DEFAULT_URL = "http://localhost:9200"
DEFAULT_INDEX = "wiki-articles"
BULK_TIMEOUT_S = 120
SETTINGS_TIMEOUT_S = 30
RESTORED_REFRESH_INTERVAL = "1s"



def add_engine_args(parser: argparse.ArgumentParser) -> None:
    """The OpenSearch-specific half, registered here so `mp_load`'s CLI can
    offer the same flags to a sharded run rather than growing its own copy."""
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--no-refresh-during-load", action="store_true",
                        help="set refresh_interval=-1 while loading, restore after "
                             "(a build-throughput tuning knob — publish it if used)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    load_driver.add_common_args(parser)
    add_engine_args(parser)
    mp_load.add_sharding_args(parser)
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


# 404 is not a failure: churn deletes ids the engine may already have dropped.
# This cannot loosen the build loader — OpenSearch sets `errors` only when some
# item failed, and the build loader sends no deletes, so a build bulk can never
# arrive here with every failure being a 404.
IGNORED_ITEM_STATUSES = (404,)


def failed_items(body: dict) -> list[dict]:
    """Item outcomes that are real failures, for BOTH bulk paths.

    One implementation because there were two: this one, and a near-copy in
    churn_load with a different 404 rule. Which statuses count as a failure
    decides whether a churn row passes its gate, and it must not depend on
    which module happened to send the bulk.
    """
    return [outcome
            for item in body.get("items", [])
            for outcome in item.values()
            if outcome.get("status", 200) >= 300
            and outcome.get("status") not in IGNORED_ITEM_STATUSES]


def first_bulk_error(body: dict) -> object:
    failures = failed_items(body)
    if not failures:
        return "unknown"
    return failures[0].get("error", failures[0])


async def send_bulk(pool: async_http.Pool, payload: bytes) -> None:
    """One `_bulk`, over one connection, with nothing else on it.

    A 2xx is not success: OpenSearch reports per-item failures inside a 200
    response, so a bulk whose items were rejected has to raise here or a failing
    engine would read as a fast one.
    """
    async with pool.acquire() as connection:
        raw = await connection.post("/_bulk", payload, "application/x-ndjson")
    body = json.loads(raw)
    if body.get("errors") and failed_items(body):
        raise RuntimeError(f"bulk request had item failures, first: {first_bulk_error(body)}")


async def attempt_bulk(pool: async_http.Pool,
                       payloads: list[bytes]) -> load_retry.Attempt:
    """One _bulk request is one retryable item. Resending it cannot duplicate
    anything: bulk_payload names every document's _id, so a repeat overwrites."""
    try:
        await send_bulk(pool, payloads[0])
    except Exception as error:
        return load_retry.Attempt(list(payloads), f"{type(error).__name__}: {error}")
    return load_retry.Attempt([])


async def send(pool: async_http.Pool, payload: bytes,
               tally: load_retry.RetryTally) -> None:
    """The engine-specific half. Runs on the dispatch loop; an outstanding bulk
    costs a connection, not a thread."""
    await load_retry.send_with_retries_async([payload],
                                             partial(attempt_bulk, pool), tally)


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


def bulk_pool(url: str, concurrency: int) -> async_http.Pool:
    """One connection per operation the driver will hold in flight, so an
    in-flight bulk always has a socket of its own and `--concurrency` is what
    the engine is actually being asked at once."""
    parsed = urlparse(url)
    return async_http.Pool(parsed.hostname or "localhost",
                           parsed.port or (443 if parsed.scheme == "https" else 80),
                           concurrency)


def build_loader(args: argparse.Namespace, url: str) -> load_driver.EngineLoader:
    return load_driver.EngineLoader(
        name="opensearch", engine="opensearch", op_kind="bulk",
        engine_version=samplers.OpenSearchSampler(url, args.index).version(),
        docs_per_operation=args.batch_size,
        encode=partial(bulk_payload, index=args.index),
        send=partial(send, bulk_pool(url, args.concurrency)),
        header_fields={
            "index": args.index,
            "refresh_during_load": not args.no_refresh_during_load,
            **mp_load.shard_header_fields(args),
        },
    )


def run_load(args: argparse.Namespace, url: str):
    return load_driver.run(args, build_loader(args, url))


def load_with_refresh_restored(args: argparse.Namespace, url: str,
                               control: requests.Session,
                               load: Callable[[], tuple[int, str]]
                               ) -> tuple[int, str]:
    if args.no_refresh_during_load:
        set_refresh_interval(control, url, args.index, "-1")
    try:
        return load()
    finally:
        if args.no_refresh_during_load:
            set_refresh_interval(control, url, args.index, RESTORED_REFRESH_INTERVAL)


def report_single_load(args: argparse.Namespace, url: str) -> tuple[int, str]:
    log, tally = run_load(args, url)
    print(f"opensearch load: {log.summary_line()}", file=sys.stderr)
    return log.summary()["errors"], tally.line()


def report_sharded_load(args: argparse.Namespace,
                        shape: mp_load.ClientShape) -> tuple[int, str]:
    """N worker processes, each POSTing its own shard over its own connections.

    The refresh knob and the closing count stay in the parent: a child setting
    `refresh_interval` would race its siblings, and N counts of one index say
    nothing N-1 of them did not.
    """
    summary = mp_load.run_sharded(args, "opensearch", shape)
    print(f"opensearch load: {mp_load.summary_line(summary)}", file=sys.stderr)
    return summary["errors"], mp_load.retries_line(summary)


def report_load(args: argparse.Namespace, url: str,
                shape: mp_load.ClientShape | None) -> tuple[int, str]:
    if shape is None:
        return report_single_load(args, url)
    return report_sharded_load(args, shape)


def main() -> int:
    args = parse_args()
    url = args.url.rstrip("/")
    load_driver.warn_if_client_bound(args.concurrency)
    control = requests.Session()
    errors, retries = load_with_refresh_restored(
        args, url, control,
        partial(report_load, args, url, mp_load.client_shape(args)))
    count = refresh_and_count(control, url, args.index)
    print(f"index '{args.index}' now holds {count} docs ({retries})",
          file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
