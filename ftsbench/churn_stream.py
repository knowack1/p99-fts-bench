"""The add/delete churn work stream, engine-agnostic and without a network.

Built once and shared by both engines on purpose. Two per-engine streams could
differ in their add/delete mix or in which ids they recycled, and that
difference would land on the S28 chart as an engine property — the same class
of defect as the two dispatch architectures `load_driver` exists to prevent.

Steady state is one delete per add, so after the ring fills the index size is
constant to within one item and each engine is doing real index-and-forget work
at a known rate. Before it fills there are no deletes to issue, which is why a
batch carries its own `op_kind`: the warm-in is genuinely a different operation
from the steady state, and a single per-loader `op_kind` would label them alike.
"""
from __future__ import annotations

import collections
import itertools
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from .load_driver import Batch

ADD = "add"
DELETE = "delete"

# Distinguishable in the artifact without touching the `latency_op` field list
# that SCHEMAS.md pins for C3, C5 and C6: the `op` string already exists and
# already varies per producer.
OP_WARM_IN = "churn_add"
OP_STEADY = "churn"


@dataclass(frozen=True)
class ChurnItem:
    """One churn operation against one document."""

    kind: str
    doc_id: str
    document: dict | None = None


def churn_id(index: int) -> str:
    """Deterministic, and deliberately restarting from 0 in every churn
    process: from the second row onward the adds overwrite ids an earlier row
    created and deleted, so the engine sees a different tombstone and merge
    load than it did on row one. That is pre-existing behaviour of every S28
    artifact on disk — changing it here would silently make new rows
    incomparable with old ones.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"churn-{index}"))


class ChurnStream:
    """Endless alternating add/delete items over a bounded ring."""

    def __init__(self, documents: Sequence[dict], ring_size: int,
                 batch_size: int) -> None:
        self._documents = itertools.cycle(documents)
        self._ring: collections.deque[str] = collections.deque()
        self._pending: collections.deque[ChurnItem] = collections.deque()
        self._ring_size = ring_size
        self._batch_size = batch_size
        self._next_index = 0
        self.adds = 0
        self.deletes = 0

    @property
    def ring_outstanding(self) -> int:
        return len(self._ring)

    def next_batch(self) -> Batch:
        items = [self._next_item() for _ in range(self._batch_size)]
        return Batch(items, op_kind=op_kind_for(items))

    def _next_item(self) -> ChurnItem:
        if self._pending:
            return self._pending.popleft()
        if len(self._ring) >= self._ring_size:
            self._pending.append(self._new_add())
            self.deletes += 1
            return ChurnItem(DELETE, self._ring.popleft())
        return self._new_add()

    def _new_add(self) -> ChurnItem:
        doc_id = churn_id(self._next_index)
        self._next_index += 1
        self._ring.append(doc_id)
        self.adds += 1
        return ChurnItem(ADD, doc_id, next(self._documents))


def op_kind_for(items: Sequence[ChurnItem]) -> str:
    return OP_STEADY if any(item.kind == DELETE for item in items) else OP_WARM_IN


def churn_source(stream: ChurnStream, duration_s: float,
                 should_stop: Callable[[], bool]):
    """A `load_driver.Source` that yields until the window closes or a signal
    arrives.

    The stop lives here rather than in the driver so a SIGTERM takes the
    driver's *normal* exit path: this generator simply stops yielding, the
    dispatch loop ends, the pool drains and records every operation still in
    flight, and both context managers close. A handler that raised instead
    would abort the drain and lose the operations it was reconciling — and
    `tools/churn_bench.sh` can send a second TERM from its EXIT trap while that
    drain is running.

    Stop latency is therefore one pacer interval plus one operation, which is
    why the driver's own schedule does the pacing and this only decides when to
    stop asking for work.
    """
    def source(args, origin_s: float) -> Iterator[Batch]:
        while not should_stop() and time.perf_counter() - origin_s < duration_s:
            yield stream.next_batch()

    return source
