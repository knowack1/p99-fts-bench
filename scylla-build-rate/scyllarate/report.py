"""Sweep results as one CSV: `#` header lines carry the facts that make the
numbers interpretable, the rows feed both charts.

Chart 1 plots `concurrency` against `docs_per_s`; chart 2 plots `concurrency`
against `p99_ms`. Both matplotlib's `loadtxt` and pandas' `read_csv` skip the
`#` lines by default.
"""
import contextlib
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TextIO

CSV_COLUMNS = ("concurrency", "docs", "errors", "wall_s",
               "docs_per_s", "p50_ms", "p99_ms")
STDOUT = "-"


@dataclass(frozen=True)
class PointResult:
    concurrency: int
    docs: int
    errors: int
    wall_s: float
    docs_per_s: float
    p50_ms: float | None
    p99_ms: float | None


def percentile(sorted_values: list[float], fraction: float) -> float | None:
    """`None`, never 0.0, when nothing succeeded. A point where every insert
    failed would otherwise plot as the best latency on the curve."""
    if not sorted_values:
        return None
    rank = math.ceil(fraction * len(sorted_values))
    return sorted_values[_clamp(rank - 1, len(sorted_values))]


def _clamp(index: int, length: int) -> int:
    return max(0, min(index, length - 1))


def _header_lines(topology, settings: dict[str, str]) -> list[str]:
    facts = {
        "scylla_version": topology.scylla_version,
        "routing": topology.routing,
        "compression": topology.compression,
        "driver": topology.driver_version,
        "protocol": topology.protocol_version,
        "reactor": topology.reactor,
        "shard_aware": topology.shard_aware,
        "shards": topology.shards,
        "tablets": topology.tablets,
        **settings,
    }
    return [f"# {key}={value}" for key, value in facts.items()]


@contextlib.contextmanager
def open_csv(destination: str) -> Iterator[TextIO]:
    """Opened before the first point runs, so an unwritable `--out` costs a
    second rather than a whole sweep."""
    if destination == STDOUT:
        yield sys.stdout
        return
    with open(destination, "w", encoding="utf-8") as handle:
        yield handle


def write_preamble(handle: TextIO, topology, settings: dict[str, str]) -> None:
    for line in _header_lines(topology, settings):
        _write_line(handle, line)
    _write_line(handle, ",".join(CSV_COLUMNS))


def append_row(handle: TextIO, result: PointResult) -> None:
    """Flushed per point: a sweep that dies at level 5 still leaves 1-4 behind."""
    _write_line(handle, _csv_row(result))


def _write_line(handle: TextIO, text: str) -> None:
    handle.write(text + "\n")
    handle.flush()


def _csv_row(result: PointResult) -> str:
    return ",".join([
        str(result.concurrency), str(result.docs), str(result.errors),
        f"{result.wall_s:.3f}", f"{result.docs_per_s:.1f}",
        _csv_latency(result.p50_ms), _csv_latency(result.p99_ms),
    ])


def _csv_latency(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def summary_table(results: list[PointResult]) -> str:
    header = f"{'conc':>6} {'docs':>9} {'err':>6} {'wall_s':>9} {'docs/s':>10} {'p50_ms':>9} {'p99_ms':>9}"
    rows = [_summary_row(result) for result in results]
    return "\n".join([header, *rows])


def _summary_row(result: PointResult) -> str:
    return (f"{result.concurrency:>6} {result.docs:>9} {result.errors:>6} "
            f"{result.wall_s:>9.2f} {result.docs_per_s:>10.1f} "
            f"{latency_text(result.p50_ms):>9} "
            f"{latency_text(result.p99_ms):>9}")


def latency_text(value: float | None) -> str:
    """A dash where nothing succeeded, so an unmeasured point cannot read as
    a fast one on stderr any more than it can in the CSV."""
    return "-" if value is None else f"{value:.2f}"


def note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
