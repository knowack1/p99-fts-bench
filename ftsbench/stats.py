"""Latency percentile helpers (linear interpolation, sorted input).

C5 plots the tail out to p99.99, which is where a percentile stops being a
summary of a distribution and starts being a rename of one or two observations.
`min_samples_for` states the sample count below which a percentile should not be
drawn at all, and `summarize_latencies` reports which of the percentiles it just
computed fall below that line. The alternative — returning a number and letting
the chart present it — is how a 300-request smoke test acquires a p99.99.

The floor is `1 / (1 - p/100)`: the sample count at which the percentile has at
least one observation at or beyond it. Below that, interpolation is reporting the
maximum under a different name. So p99 needs 100 samples, p99.9 needs 1,000 and
p99.99 needs 10,000. Meeting the floor makes the number defined, not stable —
roughly ten times the floor is needed before it stops moving between runs, which
is why C5's headline class is run at a high sample count rather than reusing a C6
window.
"""
import math
from fractions import Fraction
from typing import Any

REPORTED_PERCENTILES = (50.0, 90.0, 95.0, 99.0, 99.9, 99.99)
STABILITY_FACTOR = 10


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        raise ValueError("cannot take a percentile of no values")
    rank = (len(sorted_values) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def min_samples_for(pct: float) -> int:
    """Samples needed before `pct` describes the distribution rather than the
    maximum. See the module docstring for why this is the floor and not enough."""
    if not 0.0 <= pct < 100.0:
        raise ValueError("percentile must be in [0, 100)")
    if pct <= 0.0:
        return 1
    # Exact arithmetic: in binary floats 1 - 99.9/100 is slightly under 1/1000,
    # which rounds the p99.9 floor up to 1001 and makes the threshold arbitrary.
    return math.ceil(1 / (1 - Fraction(str(pct)) / 100))


def is_supported(count: int, pct: float) -> bool:
    """Whether a plot may draw `pct` from `count` samples. The plot layer calls
    this and omits the point; it must not silently draw an undefined tail."""
    return count >= min_samples_for(pct)


def is_stable(count: int, pct: float) -> bool:
    """Supported *and* sampled deeply enough to be reproducible between runs."""
    return count >= STABILITY_FACTOR * min_samples_for(pct)


def unsupported_percentiles(
        count: int,
        percentiles: tuple[float, ...] = REPORTED_PERCENTILES) -> list[float]:
    return [pct for pct in percentiles if not is_supported(count, pct)]


def summarize_latencies(latencies_ms: list[float]) -> dict[str, Any]:
    """Percentile summary of one latency sample. Keys `count`, `mean_ms`,
    `p50_ms`, `p95_ms`, `p99_ms` and `max_ms` are load-bearing for artifacts
    already on disk and must keep their names."""
    ordered = sorted(latencies_ms)
    return {
        "count": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "min_ms": round(ordered[0], 3),
        "p50_ms": round(percentile(ordered, 50), 3),
        "p90_ms": round(percentile(ordered, 90), 3),
        "p95_ms": round(percentile(ordered, 95), 3),
        "p99_ms": round(percentile(ordered, 99), 3),
        "p999_ms": round(percentile(ordered, 99.9), 3),
        "p9999_ms": round(percentile(ordered, 99.99), 3),
        "max_ms": round(ordered[-1], 3),
        "unsupported_percentiles": unsupported_percentiles(len(ordered)),
    }


def empty_summary() -> dict[str, Any]:
    """Zeros for a window in which nothing succeeded, so a ladder rung where the
    engine broke still appears as a point instead of a gap."""
    keys = ("mean_ms", "min_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms",
            "p999_ms", "p9999_ms", "max_ms")
    return {"count": 0, **{key: 0.0 for key in keys},
            "unsupported_percentiles": list(REPORTED_PERCENTILES)}


def summarize_or_empty(latencies_ms: list[float]) -> dict[str, Any]:
    return summarize_latencies(latencies_ms) if latencies_ms else empty_summary()
