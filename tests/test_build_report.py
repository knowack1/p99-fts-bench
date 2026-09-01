"""C1's build-series summary: the numbers the chart and its README quote.

A percentile here is quoted beside a percentile from ftsbench.stats in the same
results tree, so the two must mean the same thing.
"""
import pytest

from ftsbench import build_report, stats


def test_build_percentiles_use_the_same_definition_as_the_latency_charts():
    """The C1 sidecar's p10/p90 and a C5 p90 over the same numbers must not
    disagree: one nearest-rank and one interpolated definition in the same repo
    reads as a measurement difference rather than as two algorithms."""
    rates = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert build_report.percentile(rates, 90) == pytest.approx(
        stats.percentile(sorted(rates), 90))
    assert build_report.percentile(rates, 10) == pytest.approx(
        stats.percentile(sorted(rates), 10))


def test_build_percentile_of_an_empty_window_is_zero_not_a_crash():
    """An empty build window is a reported outcome for C1, unlike an empty
    latency sample, which stats.percentile is right to refuse."""
    assert build_report.percentile([], 90) == 0.0
