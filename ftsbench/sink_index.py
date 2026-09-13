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

**Accepted and searchable are two numbers, because on OpenSearch they are.**
`count` is what the sink took; `searchable` is what a search would find, and it
only catches up at a refresh. With `refresh_interval_s` at its default of 0 the
two are equal at every instant, which is what the vector-store half means and
what every run recorded before this existed measured. Set it and the searchable
count climbs in steps instead — the shape `osrate` has to be able to measure,
and one an accept-everything sink would otherwise never show it.
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
                 clock=time.monotonic,
                 refresh_interval_s: float = 0.0) -> None:
        self._serving_delay_s = serving_delay_s
        self._refresh_interval_s = refresh_interval_s
        self._clock = clock
        self._created_at: float | None = None
        self.count = 0
        self.adds_while_absent = 0
        self._searchable = 0
        self._refreshed_at = 0.0
        self.refresh_total = 0

    @property
    def present(self) -> bool:
        return self._created_at is not None

    def drop(self) -> None:
        self._created_at = None
        self._forget_documents()

    def create(self) -> None:
        self._created_at = self._clock()
        self._forget_documents()
        self._refreshed_at = self._created_at

    def _forget_documents(self) -> None:
        self.count = 0
        self._searchable = 0
        self.refresh_total = 0

    def add(self, docs: int) -> None:
        if not self.present:
            self.adds_while_absent += docs
            return
        self.count += docs

    @property
    def searchable(self) -> int:
        """What a search would find now.

        A negative `refresh_interval_s` is OpenSearch's `refresh_interval: -1`:
        nothing becomes visible on a timer, only an explicit `refresh()`. That
        is the setting under which a build-rate watch that waited on this number
        alone would wait forever, so the sink has to be able to produce it.
        """
        if self._due_for_refresh():
            self.refresh()
        return self._searchable

    def _due_for_refresh(self) -> bool:
        if self._refresh_interval_s < 0:
            return False
        return self._clock() - self._refreshed_at >= self._refresh_interval_s

    def refresh(self) -> None:
        """`refresh_total` counts refreshes that published something, not
        refreshes that happened.

        The scheduled ones are modelled lazily — they occur when someone looks
        — so counting every one would report how often the harness polled
        rather than how often the index turned over, and that number would move
        with `--index-interval` while nothing about the build had changed.
        """
        if self._searchable != self.count:
            self.refresh_total += 1
        self._searchable = self.count
        self._refreshed_at = self._clock()

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
