import pytest

from ftsbench import stats


def test_percentile_interpolates_between_neighbours():
    """Hand-computed: rank = (5-1) * 0.95 = 3.8, so 4 + 0.8 * (5 - 4)."""
    assert stats.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == pytest.approx(4.8)


def test_percentile_of_a_midpoint_needs_no_interpolation():
    assert stats.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == pytest.approx(3.0)


def test_percentile_at_the_extremes_returns_the_extremes():
    ordered = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert stats.percentile(ordered, 0) == pytest.approx(1.0)
    assert stats.percentile(ordered, 100) == pytest.approx(5.0)


def test_p99_of_one_hundred_values_is_hand_computable():
    """rank = 99 * 0.99 = 98.01, so values[98] + 0.01 * (values[99] - values[98])."""
    ordered = [float(n) for n in range(1, 101)]
    assert stats.percentile(ordered, 99) == pytest.approx(99.01)


def test_percentile_of_an_empty_sample_is_an_error_not_a_zero():
    with pytest.raises(ValueError):
        stats.percentile([], 50)


def test_min_samples_follows_the_one_over_one_minus_p_rule():
    assert stats.min_samples_for(99) == 100
    assert stats.min_samples_for(99.9) == 1_000
    assert stats.min_samples_for(99.99) == 10_000


def test_p9999_is_unsupported_at_one_hundred_samples():
    assert stats.is_supported(100, 99.99) is False
    assert stats.is_supported(10_000, 99.99) is True


def test_summary_flags_the_tail_percentiles_it_cannot_support():
    summary = stats.summarize_latencies([float(n) for n in range(100)])
    assert summary["unsupported_percentiles"] == [99.9, 99.99]


def test_summary_flags_nothing_when_the_sample_supports_every_percentile():
    summary = stats.summarize_latencies([1.0] * 10_000)
    assert summary["unsupported_percentiles"] == []


def test_stability_needs_ten_times_the_support_floor():
    assert stats.is_supported(1_000, 99.9) is True
    assert stats.is_stable(1_000, 99.9) is False
    assert stats.is_stable(10_000, 99.9) is True


def test_pre_existing_summary_keys_are_unchanged():
    """Artifacts already on disk and query_bench's report read these names."""
    summary = stats.summarize_latencies([1.0, 2.0, 3.0])
    for key in ("count", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"):
        assert key in summary


def test_summary_adds_the_percentiles_c5_needs():
    summary = stats.summarize_latencies([1.0, 2.0, 3.0])
    for key in ("min_ms", "p90_ms", "p999_ms", "p9999_ms"):
        assert key in summary


def test_summary_values_are_the_expected_statistics():
    summary = stats.summarize_latencies([5.0, 1.0, 3.0])
    assert summary["count"] == 3
    assert summary["min_ms"] == pytest.approx(1.0)
    assert summary["max_ms"] == pytest.approx(5.0)
    assert summary["mean_ms"] == pytest.approx(3.0)
    assert summary["p50_ms"] == pytest.approx(3.0)


def test_empty_summary_keeps_the_ladder_rung_rather_than_dropping_it():
    summary = stats.summarize_or_empty([])
    assert summary["count"] == 0
    assert summary["p99_ms"] == 0.0
    assert summary["unsupported_percentiles"] == list(stats.REPORTED_PERCENTILES)
