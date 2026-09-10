import asyncio
import uuid

import pytest

from scyllarate import report, sweep

from .fakes import FakeSession

STATEMENT = "INSERT INTO articles ..."


def some_params(count: int) -> list[tuple]:
    return [(uuid.uuid4(), n, f"title {n}", f"text {n}") for n in range(count)]


def run_point(session: FakeSession, params: list[tuple],
              concurrency: int) -> report.PointResult:
    return asyncio.run(sweep._run_point(session, STATEMENT, iter(params), concurrency))


def test_a_point_delivers_every_document_once():
    session = FakeSession()
    result = run_point(session, some_params(50), concurrency=4)
    assert (result.docs, result.errors, session.sent) == (50, 0, 50)


def test_a_point_sends_the_documents_it_was_given():
    params = some_params(10)
    session = FakeSession()
    run_point(session, params, concurrency=3)
    assert sorted(session.params_seen) == sorted(params)


def test_in_flight_never_exceeds_the_concurrency_level():
    session = FakeSession(latency_s=0.002)
    run_point(session, some_params(40), concurrency=5)
    assert session.max_in_flight <= 5


def test_in_flight_actually_reaches_the_concurrency_level():
    session = FakeSession(latency_s=0.002)
    run_point(session, some_params(40), concurrency=5)
    assert session.max_in_flight == 5


def test_a_higher_level_puts_more_requests_in_flight():
    low = FakeSession(latency_s=0.002)
    high = FakeSession(latency_s=0.002)
    run_point(low, some_params(40), concurrency=2)
    run_point(high, some_params(40), concurrency=8)
    assert high.max_in_flight > low.max_in_flight


def test_a_failed_insert_is_counted_and_the_rest_still_go():
    session = FakeSession(failing_positions=frozenset({3, 7}))
    result = run_point(session, some_params(20), concurrency=4)
    assert (result.docs, result.errors, session.sent) == (18, 2, 20)


def test_an_empty_corpus_produces_an_empty_point():
    result = run_point(FakeSession(), [], concurrency=4)
    assert (result.docs, result.errors, result.p99_ms) == (0, 0, 0.0)


def test_a_point_measures_a_positive_wall_and_rate():
    result = run_point(FakeSession(latency_s=0.001), some_params(20), concurrency=4)
    assert result.wall_s > 0 and result.docs_per_s > 0


def test_latency_reflects_the_time_the_driver_took():
    result = run_point(FakeSession(latency_s=0.02), some_params(8), concurrency=8)
    assert result.p50_ms >= 20.0


def test_the_queue_is_bounded_so_the_producer_cannot_race_ahead():
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)

    async def fill_then_measure() -> int:
        producer = asyncio.create_task(sweep._fill_queue(queue, iter(some_params(50)), 1))
        await asyncio.sleep(0)
        depth = queue.qsize()
        producer.cancel()
        return depth

    assert asyncio.run(fill_then_measure()) <= 2


def test_the_producer_stops_every_worker():
    async def collect() -> list:
        queue: asyncio.Queue = asyncio.Queue()
        await sweep._fill_queue(queue, iter(some_params(2)), workers=3)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    assert collect_sentinels(asyncio.run(collect())) == 3


def collect_sentinels(items: list) -> int:
    return sum(1 for item in items if item is sweep.STOP)


def test_a_sweep_returns_one_result_per_level_in_order():
    session = FakeSession()
    results = asyncio.run(sweep.run_sweep(
        session, STATEMENT, lambda: iter(some_params(12)), [2, 4, 8]))
    assert [result.concurrency for result in results] == [2, 4, 8]


def test_a_repeated_level_is_measured_twice():
    session = FakeSession()
    results = asyncio.run(sweep.run_sweep(
        session, STATEMENT, lambda: iter(some_params(6)), [4, 4]))
    assert len(results) == 2 and session.sent == 12


def test_each_level_reads_the_corpus_from_the_start():
    session = FakeSession()
    asyncio.run(sweep.run_sweep(
        session, STATEMENT, lambda: iter(some_params(5)), [2, 2, 2]))
    assert session.sent == 15


def test_progress_reporting_can_be_cancelled_cleanly():
    async def start_then_cancel() -> bool:
        task = asyncio.create_task(sweep._follow_progress(sweep.Counters(), 4))
        await sweep._cancel(task)
        return task.cancelled()

    assert asyncio.run(start_then_cancel())


def test_progress_reports_the_delta_since_the_last_tick(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(sweep, "note", seen.append)
    monkeypatch.setattr(sweep, "PROGRESS_INTERVAL_S", 0.01)

    async def tick_once() -> None:
        counters = sweep.Counters()
        counters.record_ok(1.0)
        counters.record_ok(1.0)
        task = asyncio.create_task(sweep._follow_progress(counters, 4))
        await asyncio.sleep(0.03)
        await sweep._cancel(task)

    asyncio.run(tick_once())
    assert seen and "2 docs/s" in seen[0]


def test_a_point_announces_its_first_failure(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(sweep, "note", seen.append)
    run_point(FakeSession(failing_positions=frozenset({1})), some_params(4), 2)
    assert any("wire is busy" in line for line in seen)


def test_a_clean_point_announces_no_failure(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(sweep, "note", seen.append)
    run_point(FakeSession(), some_params(4), 2)
    assert not any("!!" in line for line in seen)


def test_a_zero_wall_cannot_divide_by_zero():
    counters = sweep.Counters()
    counters.record_ok(1.0)
    assert sweep._summarize(1, counters, wall_s=0.0).docs_per_s == 0.0
