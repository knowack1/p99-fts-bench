"""The index the CQL sink and the vector-store sink both pretend to hold.

`scyllarate` now empties the keyspace before every concurrency level and will
not start loading until the vector-store reports the *new* index at count 0 and
status SERVING. Against an accept-and-discard sink that gate has to be
answerable, or the harness cannot be measured against the instrument built to
measure it — so the DDL arriving on the CQL side moves the state the HTTP side
reports.

**Not `AcceptedWork`.** That counter is cumulative for the whole process: it
feeds the sink's own summary line and `--stats-out`, and a `DROP KEYSPACE`
zeroing it would make a six-level ladder report the documents of its last level
as the documents of the run. This one is per-index and resets with the index.

**Documents arriving while no index exists are not counted.** The harness
creates the index before it loads, so an add against an absent index means the
loader and the sink disagree about the lifecycle — recorded, not silently
absorbed, for the same reason the sinks record unexpected routes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

SERVING = "SERVING"
BUILDING = "BUILDING"


@dataclass(frozen=True)
class IndexStatus:
    count: int
    status: str

    def as_json(self) -> dict[str, object]:
        return {"count": self.count, "status": self.status}


class ModelledIndex:
    """One index's lifecycle: absent, then building, then serving."""

    def __init__(self, serving_delay_s: float = 0.0,
                 clock=time.monotonic) -> None:
        self._serving_delay_s = serving_delay_s
        self._clock = clock
        self._created_at: float | None = None
        self.count = 0
        self.adds_while_absent = 0

    @property
    def present(self) -> bool:
        return self._created_at is not None

    def drop(self) -> None:
        self._created_at = None
        self.count = 0

    def create(self) -> None:
        self._created_at = self._clock()
        self.count = 0

    def add(self, docs: int) -> None:
        if not self.present:
            self.adds_while_absent += docs
            return
        self.count += docs

    def status(self) -> IndexStatus | None:
        """`None` where the real vector-store answers 404: no such index."""
        if self._created_at is None:
            return None
        return IndexStatus(self.count, self._phase())

    def _phase(self) -> str:
        assert self._created_at is not None
        if self._clock() - self._created_at < self._serving_delay_s:
            return BUILDING
        return SERVING
