import time

import pytest

from ftsbench import pacer


def test_schedule_offsets_are_absolute_and_evenly_spaced():
    assert pacer.schedule(rate_per_s=100.0, count=4) == pytest.approx(
        [0.0, 0.01, 0.02, 0.03])


def test_schedule_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        pacer.schedule(rate_per_s=0.0, count=1)


def test_schedule_does_not_drift_when_work_is_slow():
    """The property that defeats coordinated omission: intended times are fixed
    up front, so a slow operation cannot push later ones later."""
    origin = 1000.0
    ops = [
        pacer.Op(i=i, t_intended_s=origin + offset)
        for i, offset in enumerate(pacer.schedule(50.0, 5))
    ]
    assert [op.t_intended_s for op in ops] == pytest.approx(
        [1000.0, 1000.02, 1000.04, 1000.06, 1000.08])


def test_paced_yields_requested_count_on_the_expected_schedule():
    origin = time.perf_counter()
    ops = list(pacer.paced(rate_per_s=500.0, count=5, origin_s=origin))
    assert [op.i for op in ops] == [0, 1, 2, 3, 4]
    assert [op.t_intended_s - origin for op in ops] == pytest.approx(
        [0.0, 0.002, 0.004, 0.006, 0.008])


def test_paced_does_not_skip_operations_when_behind_schedule():
    """Started in the past, every operation is already overdue. All of them must
    still be yielded: the backlog is the measurement, not something to drop."""
    origin = time.perf_counter() - 10.0
    ops = list(pacer.paced(rate_per_s=1000.0, count=200, origin_s=origin))
    assert len(ops) == 200
    assert all(op.t_intended_s < time.perf_counter() for op in ops)


def test_paced_for_duration_covers_the_window_and_stops():
    origin = time.perf_counter() - 10.0
    ops = list(pacer.paced_for_duration(rate_per_s=100.0, duration_s=1.0,
                                        origin_s=origin))
    assert len(ops) == 100
    assert max(op.t_intended_s for op in ops) - origin < 1.0


def test_unpaced_marks_intended_as_now_so_latency_equals_service():
    ops = list(pacer.unpaced(3))
    assert [op.i for op in ops] == [0, 1, 2]


def test_latency_is_measured_from_intended_not_actual_start():
    latency_ms, service_ms, queue_ms = pacer.latencies_ms(
        t_intended_s=1.000, t_start_s=1.050, t_end_s=1.060)
    assert latency_ms == pytest.approx(60.0)
    assert service_ms == pytest.approx(10.0)
    assert queue_ms == pytest.approx(50.0)


def test_queue_time_is_zero_when_dispatch_was_on_time():
    latency_ms, service_ms, queue_ms = pacer.latencies_ms(
        t_intended_s=2.0, t_start_s=2.0, t_end_s=2.007)
    assert latency_ms == pytest.approx(service_ms)
    assert queue_ms == pytest.approx(0.0)


def test_queue_time_never_goes_negative_on_early_dispatch():
    _, _, queue_ms = pacer.latencies_ms(
        t_intended_s=5.0, t_start_s=4.9995, t_end_s=5.010)
    assert queue_ms == 0.0
