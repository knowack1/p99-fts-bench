"""What survives a sweep that goes wrong: the points already measured, and the
difference between "no latency was measured" and "the latency was zero".
"""
import argparse
import asyncio
import json
import uuid

import pytest

from scyllarate import report, sweep
from scyllarate.__main__ import _announce_abort, _collector, _exit_code, _sweep

from .fakes import FakeSession, a_point, a_topology

STATEMENT = "INSERT INTO articles ..."


def some_params(count: int) -> list[tuple]:
    return [(uuid.uuid4(), n, f"title {n}", f"text {n}") for n in range(count)]


def a_source(count: int):
    return lambda: iter(some_params(count))


def a_failing_source(good_docs: int, levels_before_failure: int):
    state = {"level": 0}

    def factory():
        state["level"] += 1
        doomed = state["level"] > levels_before_failure
        return _maybe_truncated(good_docs, doomed)

    return factory


def _maybe_truncated(count: int, doomed: bool):
    for params in some_params(count):
        yield params
    if doomed:
        raise ValueError("truncated JSONL line 4242")


def _sweep_into(handle, source_factory, levels, session=None) -> list:
    results: list = []
    asyncio.run(sweep.run_sweep(session or FakeSession(), STATEMENT,
                                source_factory, levels, _collector(results, handle)))
    return results


def test_points_measured_before_a_mid_sweep_failure_are_still_on_disk(tmp_path):
    destination = tmp_path / "sweep.csv"
    with report.open_csv(str(destination)) as handle:
        report.write_preamble(handle, a_topology(), {})
        with pytest.raises(ValueError):
            _sweep_into(handle, a_failing_source(4, levels_before_failure=2), [2, 4, 8])
    rows = _measured_rows(destination.read_text())
    assert [row.split(",")[0] for row in rows] == ["2", "4"]


def test_a_point_reaches_the_file_before_the_next_one_starts(tmp_path):
    destination = tmp_path / "sweep.csv"
    seen_after_first: list[str] = []
    with report.open_csv(str(destination)) as handle:
        report.write_preamble(handle, a_topology(), {})
        report.append_row(handle, a_point(8))
        seen_after_first.extend(_measured_rows(destination.read_text()))
        report.append_row(handle, a_point(16))
    assert len(seen_after_first) == 1


def test_an_unwritable_destination_fails_before_any_point_runs():
    with pytest.raises(OSError):
        with report.open_csv("/nonexistent-dir/sweep.csv"):
            pytest.fail("the sweep must never start against a bad destination")


def test_a_broken_progress_printer_does_not_destroy_a_measured_point(monkeypatch):
    def broken(message: str) -> None:
        raise BrokenPipeError("stderr went away")

    monkeypatch.setattr(sweep, "note", broken)
    monkeypatch.setattr(sweep, "PROGRESS_INTERVAL_S", 0.001)
    result = asyncio.run(sweep._measure_at_concurrency(
        FakeSession(latency_s=0.005), STATEMENT, iter(some_params(20)), 4))
    assert (result.docs, result.errors) == (20, 0)


def test_a_failed_producer_leaves_no_task_behind():
    async def run_and_count() -> int:
        with pytest.raises(ValueError):
            await sweep._measure_at_concurrency(
                FakeSession(latency_s=0.01), STATEMENT,
                _maybe_truncated(4, doomed=True), 4)
        return len([task for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()])

    assert asyncio.run(run_and_count()) == 0


def test_a_point_that_delivered_nothing_reports_no_latency():
    session = FakeSession(failing_positions=frozenset(range(1, 11)))
    result = asyncio.run(sweep._measure_at_concurrency(
        session, STATEMENT, iter(some_params(10)), 4))
    assert (result.docs, result.errors) == (0, 10)
    assert result.p50_ms is None and result.p99_ms is None


def test_an_unmeasured_latency_is_an_empty_csv_cell_not_a_zero():
    row = report._csv_row(a_point(8, p50_ms=None, p99_ms=None)).split(",")
    assert (row[5], row[6]) == ("", "")


def test_an_unmeasured_latency_reads_as_a_dash_in_the_summary():
    line = report.summary_table([a_point(8, p50_ms=None, p99_ms=None)]).splitlines()[1]
    assert line.split()[-2:] == ["-", "-"]


def test_an_aborted_sweep_exits_non_zero_even_if_every_point_was_clean():
    assert _exit_code([a_point(8)], aborted=True) == 1


def test_a_complete_clean_sweep_exits_zero():
    assert _exit_code([a_point(8)], aborted=False) == 0


def _measured_rows(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return lines[1:]


def an_args(tmp_path, corpus_text: str, levels: list[int]) -> argparse.Namespace:
    path = tmp_path / "corpus.jsonl"
    path.write_text(corpus_text)
    return argparse.Namespace(corpus=str(path), max_docs=0, concurrency=levels,
                              out=str(tmp_path / "sweep.csv"))


def a_corpus_line(page_id: int) -> str:
    return json.dumps({"id": page_id,
                       "uuid": "00000000-0000-5000-8000-000000000000",
                       "title": "t", "text": "x"}) + "\n"


def test_a_readable_corpus_sweeps_without_aborting(tmp_path):
    results: list = []
    aborted = _sweep(FakeSession(), STATEMENT,
                     an_args(tmp_path, a_corpus_line(1) * 4, [2, 4]),
                     results.append)
    assert aborted is False
    assert [result.concurrency for result in results] == [2, 4]


def test_a_malformed_corpus_line_aborts_the_sweep_without_a_traceback(tmp_path, capsys):
    results: list = []
    aborted = _sweep(FakeSession(), STATEMENT,
                     an_args(tmp_path, a_corpus_line(1) + "{not json\n", [2]),
                     results.append)
    assert aborted is True
    assert "sweep aborted" in capsys.readouterr().err


def test_an_abort_says_where_the_measured_levels_are(capsys):
    _announce_abort(ValueError("truncated"), "/runs/sweep.csv")
    assert "/runs/sweep.csv" in capsys.readouterr().err


def test_an_interrupted_sweep_keeps_the_levels_it_measured(tmp_path, capsys):
    def interrupt_after_first(result):
        results.append(result)
        raise KeyboardInterrupt

    results: list = []
    aborted = _sweep(FakeSession(), STATEMENT,
                     an_args(tmp_path, a_corpus_line(1) * 4, [2, 4]),
                     interrupt_after_first)
    assert aborted is True
    assert [result.concurrency for result in results] == [2]
    assert "sweep aborted" in capsys.readouterr().err
