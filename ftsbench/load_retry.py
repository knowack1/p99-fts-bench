"""One retry policy, shared by both loaders.

A single `ConnectionBusy` — the driver's own in-flight limit, not the engine
refusing the write — cost the 2026-08-19 campaign two ScyllaDB CDC repetitions.
`execute_concurrent_with_args(..., raise_on_first_error=True)` raised on the
first failed row of a 500-row batch, so the rows not yet submitted were never
sent and never retried: one repetition finished 1 document short, another 380.
The index-completeness gate caught both, which is the only reason it was not
published as ScyllaDB losing documents.

Both loaders retry through this module rather than each having its own policy.
`COMPARABILITY.md` asks for the two ingest paths to differ in what the engines
do, not in how hard the client tries, and "OpenSearch happened to have no
failures in this campaign" is not the same commitment as a shared policy.

A retry is a disclosed condition of the run, not a repair: `RetryTally` is what
the loader's closing line reports, so a client that was overloaded says so
instead of quietly producing a slightly slower number.
"""
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Sequence


class Attempt(NamedTuple):
    """What one send did. `failed` empty means everything landed."""
    failed: list[Any]
    error: str = ""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    backoff_s: float = 0.05


DEFAULT_POLICY = RetryPolicy()


class RetryTally:
    """Accumulates across every batch of one load, like LatencyLog does."""

    def __init__(self) -> None:
        self._items = 0
        self._retries = 0
        # opensearch_load retries inside its worker threads.
        self._lock = threading.Lock()

    def note(self, items: int) -> None:
        with self._lock:
            self._items += items
            self._retries += 1

    def summary(self) -> dict[str, int]:
        return {"retried_items": self._items, "retries": self._retries}

    def line(self) -> str:
        if not self._retries:
            return "no retries"
        return (f"{self._items} item(s) retried over {self._retries} retr"
                f"{'y' if self._retries == 1 else 'ies'} — the client was "
                "overloaded, which is a condition of this run")


class RetriesExhausted(RuntimeError):
    """Raised so the loader exits non-zero: documents that never landed must
    fail the load, not shorten it."""


def send_with_retries(items: Sequence[Any], send: Callable[[list[Any]], Attempt],
                      tally: RetryTally, policy: RetryPolicy = DEFAULT_POLICY,
                      sleep: Callable[[float], None] = time.sleep) -> None:
    remaining = list(items)
    for attempt in range(policy.attempts):
        outcome = send(remaining)
        if not outcome.failed:
            return
        if attempt + 1 == policy.attempts:
            raise RetriesExhausted(
                f"{len(outcome.failed)} of {len(items)} item(s) still failing "
                f"after {policy.attempts} attempts: {outcome.error}")
        tally.note(len(outcome.failed))
        sleep(policy.backoff_s * (2 ** attempt))
        remaining = outcome.failed
