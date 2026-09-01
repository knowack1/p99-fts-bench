import json
import time

import pytest

from ftsbench import load_gen, pacer, stats, sweep

HEALTHY_CEILING_QPS = 1000.0


class FakeEngine:
    """Records the queries it was asked for and answers from a fixed script, so
    the generator can be exercised without OpenSearch or ScyllaDB running."""

    def __init__(self, hits: int = 3, fail_on: tuple[str, ...] = (),
                 delay_s: float = 0.0):
        self._hits = hits
        self._fail_on = fail_on
        self._delay_s = delay_s
        self.seen: list[str] = []

    def search(self, query_text: str, limit: int = 10) -> list[str]:
        self.seen.append(query_text)
        if self._delay_s:
            time.sleep(self._delay_s)
        if query_text in self._fail_on:
            raise RuntimeError("connection refused")
        return [f"doc-{n}" for n in range(self._hits)]


def query_set(**classes: list[str]) -> dict:
    return {"corpus": "data/corpus.jsonl", "classes": classes}


def plan_of(*texts: str) -> list[load_gen.Query]:
    return [load_gen.Query("rare_term", i, text) for i, text in enumerate(texts)]


def overdue_ops(count: int, behind_s: float) -> list[pacer.Op]:
    """Ops whose intended start is already in the past, so every one of them
    carries a known queueing delay without needing a slow engine."""
    origin_s = time.perf_counter() - behind_s
    return list(pacer.paced(rate_per_s=10_000.0, count=count, origin_s=origin_s))


def tally_of(records: list[dict]) -> load_gen.RunTally:
    tally = load_gen.RunTally()
    for record in records:
        tally.add(record)
    return tally


def record(latency_ms: float, service_ms: float, ok: bool = True) -> dict:
    return {"latency_ms": latency_ms, "service_ms": service_ms,
            "queue_ms": latency_ms - service_ms, "class": "rare_term",
            "ok": ok, "error": None if ok else "RuntimeError: boom"}


def test_percentiles_come_from_latency_not_service_time():
    """The whole point of the open-loop design: a request that waited 95 ms in
    the generator and 5 ms in the engine is a 100 ms request."""
    tally = tally_of([record(latency_ms=100.0, service_ms=5.0)] * 10)
    assert stats.summarize_latencies(tally.latency_ms)["p50_ms"] == pytest.approx(100.0)
    assert stats.summarize_latencies(tally.service_ms)["p50_ms"] == pytest.approx(5.0)


def test_a_failed_query_is_counted_as_an_error_and_left_out_of_percentiles():
    tally = tally_of([record(10.0, 10.0), record(9999.0, 9999.0, ok=False),
                      record(12.0, 12.0)])
    assert tally.errors == 1
    assert tally.latency_ms == [10.0, 12.0]
    assert stats.summarize_latencies(tally.latency_ms)["max_ms"] == pytest.approx(12.0)


def test_the_first_error_message_is_kept_for_the_run_summary():
    tally = tally_of([record(1.0, 1.0, ok=False), record(2.0, 2.0, ok=False)])
    assert tally.first_error == "RuntimeError: boom"


def test_completed_counts_errors_so_a_failing_engine_is_not_called_slow():
    tally = tally_of([record(1.0, 1.0), record(2.0, 2.0, ok=False)])
    assert tally.completed == 2


def test_per_class_latencies_are_kept_separately_for_c6():
    tally = load_gen.RunTally()
    tally.add({**record(10.0, 10.0), "class": "phrase"})
    tally.add({**record(20.0, 20.0), "class": "bool_and"})
    assert tally.per_class_latency_ms == {"phrase": [10.0], "bool_and": [20.0]}


def test_latency_op_record_matches_the_schema_for_a_search():
    op = pacer.Op(i=7, t_intended_s=100.0)
    outcome = load_gen.Outcome(t_start_s=100.05, t_end_s=100.06, hits=3, error=None)
    result = load_gen.latency_op_record(op, plan_of("kernel")[0], outcome,
                                       origin_s=100.0)
    assert result["record"] == "latency_op"
    assert result["op"] == "search"
    assert result["n_docs"] is None
    assert result["class"] == "rare_term"
    assert result["latency_ms"] == pytest.approx(60.0)
    assert result["service_ms"] == pytest.approx(10.0)
    assert result["queue_ms"] == pytest.approx(50.0)
    assert result["ok"] is True


def test_latency_op_record_marks_a_failure_without_hits():
    op = pacer.Op(i=1, t_intended_s=5.0)
    outcome = load_gen.Outcome(5.0, 5.001, None, "RuntimeError: connection refused")
    result = load_gen.latency_op_record(op, plan_of("kernel")[0], outcome, 5.0)
    assert result["ok"] is False
    assert result["hits"] is None
    assert result["error"] == "RuntimeError: connection refused"


def test_drive_ops_measures_the_backlog_it_started_with():
    engine = FakeEngine()
    tally = load_gen.drive_ops(engine, plan_of("kernel", "syscall"),
                               overdue_ops(20, behind_s=0.5),
                               load_gen.GeneratorSettings(concurrency=4),
                               origin_s=time.perf_counter())
    assert len(tally.latency_ms) == 20
    assert min(tally.latency_ms) > 400.0
    assert max(tally.service_ms) < 100.0
    assert min(tally.queue_ms) > 400.0


def test_drive_ops_records_engine_failures_as_errors():
    engine = FakeEngine(fail_on=("syscall",))
    tally = load_gen.drive_ops(engine, plan_of("syscall"), overdue_ops(5, 0.01),
                               load_gen.GeneratorSettings(concurrency=2),
                               origin_s=time.perf_counter())
    assert tally.errors == 5
    assert tally.latency_ms == []
    assert tally.first_error.startswith("RuntimeError")


def test_drive_ops_writes_one_jsonl_record_per_operation(tmp_path):
    log = tmp_path / "c5-fake-1.jsonl"
    with open(log, "w", encoding="utf-8") as sink:
        load_gen.drive_ops(FakeEngine(), plan_of("kernel"), overdue_ops(6, 0.01),
                           load_gen.GeneratorSettings(concurrency=2),
                           origin_s=time.perf_counter(), sink=sink)
    written = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(written) == 6
    assert all(item["record"] == "latency_op" for item in written)


def test_run_window_stops_at_the_end_of_the_window():
    """Tolerance of one op: the schedule is absolute against a large
    perf_counter origin, so the boundary op can land either side of it."""
    engine = FakeEngine()
    tally = load_gen.run_window(engine, plan_of("kernel"), rate_per_s=200.0,
                                duration_s=0.1,
                                settings=load_gen.GeneratorSettings(concurrency=4))
    assert tally.completed == pytest.approx(20, abs=1)


def test_calibration_uses_a_query_that_matches_nothing():
    engine = FakeEngine(hits=0)
    calibration = load_gen.calibrate(engine, load_gen.GeneratorSettings(concurrency=2),
                                     duration_s=0.05)
    assert calibration.calibration_hits == 0
    assert calibration.generator_ceiling_qps > 0
    assert calibration.dispatch_ceiling_qps > 0
    assert set(engine.seen) == {load_gen.UNMATCHABLE_QUERY}


def test_calibration_reports_a_failed_probe_rather_than_zero_hits():
    engine = FakeEngine(fail_on=(load_gen.UNMATCHABLE_QUERY,))
    calibration = load_gen.calibrate(engine, load_gen.GeneratorSettings(concurrency=1),
                                     duration_s=0.02)
    assert calibration.calibration_hits == load_gen.PROBE_FAILED_HITS


def test_generator_saturated_is_false_on_a_healthy_point():
    assert load_gen.is_generator_saturated(
        offered_qps=100.0, achieved_qps=99.5, queue_p99_ms=1.0, p99_ms=20.0,
        generator_ceiling_qps=HEALTHY_CEILING_QPS,
        completed_ops=100) is False


def test_generator_saturated_fires_when_the_offered_rate_nears_the_ceiling():
    assert load_gen.is_generator_saturated(
        offered_qps=600.0, achieved_qps=598.0, queue_p99_ms=1.0, p99_ms=20.0,
        generator_ceiling_qps=HEALTHY_CEILING_QPS,
        completed_ops=100) is True


def test_generator_saturated_fires_when_the_offered_rate_was_not_delivered():
    assert load_gen.is_generator_saturated(
        offered_qps=100.0, achieved_qps=80.0, queue_p99_ms=1.0, p99_ms=20.0,
        generator_ceiling_qps=HEALTHY_CEILING_QPS,
        completed_ops=100) is True


def test_generator_saturated_fires_when_the_generator_held_the_requests():
    assert load_gen.is_generator_saturated(
        offered_qps=100.0, achieved_qps=99.5, queue_p99_ms=10.0, p99_ms=20.0,
        generator_ceiling_qps=HEALTHY_CEILING_QPS,
        completed_ops=100) is True


def test_generator_saturated_fires_when_no_operation_succeeded():
    """An engine shedding load rejects fast, so the offered rate is still met and
    every percentile is zero over an empty sample. Without this the rung is the
    lowest p99 on the ladder and wins the C7 knee at the highest rate tested."""
    assert load_gen.is_generator_saturated(
        offered_qps=5000.0, achieved_qps=4999.0, queue_p99_ms=0.0, p99_ms=0.0,
        generator_ceiling_qps=HEALTHY_CEILING_QPS * 100,
        completed_ops=0) is True


def test_class_filter_restricts_the_mix_to_one_class():
    plan = load_gen.build_plan(query_set(rare_term=["kernel"], phrase=['"a b"']),
                               class_filter="phrase")
    assert [query.query_class for query in plan] == ["phrase"]
    assert [query.text for query in plan] == ['"a b"']


def test_unfiltered_plan_covers_every_non_empty_class():
    plan = load_gen.build_plan(
        query_set(rare_term=["kernel"], phrase=[], bool_and=["a AND b"]), None)
    assert sorted(query.query_class for query in plan) == ["bool_and", "rare_term"]


def test_an_unknown_class_is_an_error_not_an_empty_run():
    with pytest.raises(ValueError):
        load_gen.build_plan(query_set(rare_term=["kernel"]), "no_such_class")


def test_an_empty_selection_is_an_error_not_an_empty_run():
    with pytest.raises(ValueError):
        load_gen.build_plan(query_set(rare_term=[]), None)


def test_queue_capacity_is_bounded_by_the_worker_count():
    assert load_gen.queue_capacity(16) == 16 * load_gen.QUEUE_DEPTH_PER_WORKER
    assert load_gen.queue_capacity(0) == 1


def test_queue_p99_is_zero_when_nothing_succeeded():
    assert load_gen.queue_p99_ms(load_gen.RunTally()) == 0.0


def sweep_args(**overrides) -> object:
    defaults = {"duration": 10.0, "warmup": 2.0}
    return type("Args", (), {**defaults, **overrides})()


def test_sweep_point_carries_every_field_the_c7_contract_names():
    tally = tally_of([record(10.0, 9.0)] * 100)
    point = sweep.sweep_point_record(
        3, 400.0, tally, sweep_args(),
        load_gen.Calibration(HEALTHY_CEILING_QPS, HEALTHY_CEILING_QPS, 16, 10.0, 0))
    for key in ("record", "i", "offered_qps", "achieved_qps", "duration_s",
                "warmup_s", "count", "errors", "p50_ms", "p95_ms", "p99_ms",
                "p999_ms", "max_ms", "queue_p99_ms", "generator_ceiling_qps",
                "generator_saturated"):
        assert key in point
    assert point["record"] == "sweep_point"
    assert point["generator_ceiling_qps"] == HEALTHY_CEILING_QPS


def test_sweep_point_percentiles_ignore_service_time():
    tally = tally_of([record(latency_ms=100.0, service_ms=1.0)] * 100)
    point = sweep.sweep_point_record(
        0, 10.0, tally, sweep_args(),
        load_gen.Calibration(HEALTHY_CEILING_QPS, HEALTHY_CEILING_QPS, 16, 10.0, 0))
    assert point["p50_ms"] == pytest.approx(100.0)
    assert point["queue_p99_ms"] == pytest.approx(99.0)


def test_a_rung_where_everything_failed_is_still_a_point():
    tally = tally_of([record(1.0, 1.0, ok=False)] * 5)
    point = sweep.sweep_point_record(
        0, 100.0, tally, sweep_args(),
        load_gen.Calibration(HEALTHY_CEILING_QPS, HEALTHY_CEILING_QPS, 16, 10.0, 0))
    assert point["count"] == 0
    assert point["errors"] == 5
    assert point["p99_ms"] == 0.0


def test_the_ladder_is_geometric_so_it_reaches_the_knee_in_few_rungs():
    assert sweep.geometric_ladder(25.0, 2.0, 200.0) == [25.0, 50.0, 100.0, 200.0]


def test_an_explicit_ladder_overrides_the_geometric_one():
    args = sweep_args(rates="10,20,45")
    assert sweep.build_ladder(args) == [10.0, 20.0, 45.0]


def test_a_ladder_that_cannot_climb_is_rejected():
    with pytest.raises(ValueError):
        sweep.geometric_ladder(25.0, 1.0, 200.0)


def test_an_overridden_ceiling_is_marked_as_not_measured():
    calibration = sweep.establish_ceiling(
        FakeEngine(), load_gen.GeneratorSettings(concurrency=2),
        sweep_args(ceiling_qps=2500.0))
    assert calibration.generator_ceiling_qps == 2500.0
    assert calibration.dispatch_ceiling_qps == sweep.CEILING_NOT_MEASURED
