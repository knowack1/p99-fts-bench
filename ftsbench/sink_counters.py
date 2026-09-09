"""What a null sink accepted, and what it was asked that it did not expect.

Shared by both sink modes so the HTTP and CQL halves cannot disagree about what
"one operation" and "one document" mean — the same reason `load_driver` owns the
loaders' schedule rather than each loader owning its own.

Counting is on the hot path, so it is two integer adds behind no lock: both
servers are single-threaded asyncio, and the reporter reads the counters from a
task on the same loop. A lock here would put contention inside the thing the
measurement is trying to leave unconstrained.

Unexpected requests are recorded rather than merely refused. A sink that
answered 404 in silence would let a loader change land as a throughput
difference: the run would still complete, the number would still look like a
client ceiling, and nothing in the artifacts would say the setup call never
arrived.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkSnapshot:
    ops: int
    docs: int
    elapsed_s: float

    @property
    def ops_per_s(self) -> float:
        return self.ops / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def docs_per_s(self) -> float:
        return self.docs / self.elapsed_s if self.elapsed_s > 0 else 0.0


class AcceptedWork:
    """Accept-and-discard accounting for one sink process."""

    def __init__(self) -> None:
        self.ops = 0
        self.docs = 0
        self.unexpected: Counter[str] = Counter()
        self._origin_s = time.perf_counter()

    def add(self, ops: int, docs: int) -> None:
        self.ops += ops
        self.docs += docs

    def note_unexpected(self, what: str) -> None:
        self.unexpected[what] += 1

    def snapshot(self) -> WorkSnapshot:
        return WorkSnapshot(self.ops, self.docs,
                            time.perf_counter() - self._origin_s)

    def summary(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {
            "ops_accepted": snapshot.ops,
            "docs_accepted": snapshot.docs,
            "sink_wall_s": round(snapshot.elapsed_s, 3),
            "sink_ops_per_s": round(snapshot.ops_per_s, 1),
            "sink_docs_per_s": round(snapshot.docs_per_s, 1),
            "unexpected_requests": dict(self.unexpected),
        }


def summary_line(snapshot: WorkSnapshot) -> str:
    return (f"sink: {snapshot.docs} docs, {snapshot.ops} ops in "
            f"{snapshot.elapsed_s:.1f}s "
            f"({snapshot.docs_per_s:.0f} docs/s, {snapshot.ops_per_s:.0f} ops/s)")


async def report_periodically(work: AcceptedWork, interval_s: float) -> None:
    """Progress on stderr, off the request path.

    The sink's own rate is not the measurement — the loader's artifact is — but
    a sink that printed nothing would leave a stalled run indistinguishable from
    a slow one for the length of the point.
    """
    while True:
        await asyncio.sleep(interval_s)
        print(summary_line(work.snapshot()), file=sys.stderr)


def write_stats(path: str, work: AcceptedWork, header: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({**header, **work.summary()}, stream, indent=2)
        stream.write("\n")
