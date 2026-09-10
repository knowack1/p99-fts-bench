"""Sweep results as one CSV: `#` header lines carry the facts that make the
numbers interpretable, the rows feed both charts.

Chart 1 plots `concurrency` against `docs_per_s`; chart 2 plots `concurrency`
against `p99_ms`. Both matplotlib's `loadtxt` and pandas' `read_csv` skip the
`#` lines by default.
"""
import math
import sys
from dataclasses import dataclass

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
    p50_ms: float
    p99_ms: float


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
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


def csv_text(topology, settings: dict[str, str],
             results: list[PointResult]) -> str:
    lines = _header_lines(topology, settings)
    lines.append(",".join(CSV_COLUMNS))
    lines.extend(_csv_row(result) for result in results)
    return "\n".join(lines) + "\n"


def _csv_row(result: PointResult) -> str:
    return ",".join([
        str(result.concurrency), str(result.docs), str(result.errors),
        f"{result.wall_s:.3f}", f"{result.docs_per_s:.1f}",
        f"{result.p50_ms:.3f}", f"{result.p99_ms:.3f}",
    ])


def summary_table(results: list[PointResult]) -> str:
    header = f"{'conc':>6} {'docs':>9} {'err':>6} {'wall_s':>9} {'docs/s':>10} {'p50_ms':>9} {'p99_ms':>9}"
    rows = [_summary_row(result) for result in results]
    return "\n".join([header, *rows])


def _summary_row(result: PointResult) -> str:
    return (f"{result.concurrency:>6} {result.docs:>9} {result.errors:>6} "
            f"{result.wall_s:>9.2f} {result.docs_per_s:>10.1f} "
            f"{result.p50_ms:>9.2f} {result.p99_ms:>9.2f}")


def write_csv(text: str, destination: str) -> None:
    if destination == STDOUT:
        sys.stdout.write(text)
        return
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(text)


def note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
