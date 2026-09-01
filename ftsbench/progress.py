"""Throughput reporting shared by the two loaders.

Reports both an instantaneous rate (docs/s since the previous report) and a
cumulative average. The instantaneous rate is the one that matters for chart
C1: a cumulative average mathematically smooths away the OpenSearch
segment-merge sawtooth, which is the effect C1 exists to show.
"""
import sys
import time

DEFAULT_REPORT_EVERY_DOCS = 5_000


class ThroughputReporter:
    def __init__(self, label: str, every: int = DEFAULT_REPORT_EVERY_DOCS):
        self._label = label
        self._every = every
        self._count = 0
        self._started = time.perf_counter()
        self._last_count = 0
        self._last_at = self._started

    @property
    def count(self) -> int:
        return self._count

    def add(self, docs: int) -> None:
        milestones_before = self._count // self._every
        self._count += docs
        if self._count // self._every != milestones_before:
            self._report()

    def finish(self) -> None:
        self._report()

    def _report(self) -> None:
        now = time.perf_counter()
        elapsed = now - self._started
        window = now - self._last_at
        window_docs = self._count - self._last_count
        cumulative = self._count / elapsed if elapsed > 0 else 0.0
        instant = window_docs / window if window > 0 else 0.0
        print(
            f"{self._label}: {self._count} docs in {elapsed:.1f}s "
            f"({instant:.0f} docs/s now, {cumulative:.0f} docs/s avg)",
            file=sys.stderr,
        )
        self._last_count = self._count
        self._last_at = now
