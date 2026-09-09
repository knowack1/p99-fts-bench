"""The retry policy both loaders share, and the defect it exists to close.

The 2026-08-19 campaign lost two ScyllaDB CDC repetitions to a single
client-side `ConnectionBusy`: the driver was asked to raise on the first failed
row of a 500-row batch, so the rows behind it were never submitted at all. The
gate caught the shortfall, so it was never published as ScyllaDB losing
documents — but a gate is the last line, not the fix.

What these tests pin down is therefore not "a retry happens" but the two things
that made that loss possible: that the rest of a batch is still sent after one
row fails, and that rows which never land fail the load instead of shortening
it.
"""
import asyncio
import sys
from pathlib import Path

import pytest

from . import write_path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functools import partial

from ftsbench import (load_driver, load_retry, opensearch_load,
                      scylla_load)

ROWS = [("doc-1",), ("doc-2",), ("doc-3",), ("doc-4",)]
MIDDLE_ROW = ROWS[1]


class ConnectionBusy(Exception):
    """Named for the driver's own error: the client's in-flight limit, not the
    engine refusing the write."""


class FlakyDriver:
    """Stands in for the CQL dispatch, failing nominated rows once.

    Records every attempt it was handed, which is what makes the defect
    visible: the first recorded attempt must contain every row, not just the
    rows up to the failure.
    """

    def __init__(self, failing: set[tuple], forever: bool = False) -> None:
        self.failing = failing
        self.forever = forever
        self.attempts: list[list[tuple]] = []

    def outcome_for(self, row: tuple) -> tuple:
        if row not in self.failing:
            return (True, None)
        if not self.forever:
            self.failing = self.failing - {row}
        return (False, ConnectionBusy("too many requests already in flight"))

    async def __call__(self, session, statements) -> list:
        rows = [params for _statement, params in statements]
        self.attempts.append(rows)
        return [self.outcome_for(row) for row in rows]


def scylla_batch(monkeypatch, driver: FlakyDriver,
                 rows: list[tuple] = ROWS) -> load_retry.RetryTally:
    monkeypatch.setattr(scylla_load, "execute_all", driver)
    tally = load_retry.RetryTally()
    attempt = partial(scylla_load.attempt_rows, None, None)
    asyncio.run(scylla_load.send(attempt, rows, tally))
    return tally


def test_the_rest_of_a_batch_is_still_sent_after_one_row_fails(monkeypatch):
    driver = FlakyDriver({MIDDLE_ROW})
    scylla_batch(monkeypatch, driver)
    assert driver.attempts[0] == ROWS, \
        "rows behind the failure were abandoned — this is the campaign defect"


def test_only_the_rows_that_failed_are_resent(monkeypatch):
    driver = FlakyDriver({MIDDLE_ROW})
    scylla_batch(monkeypatch, driver)
    assert driver.attempts[1:] == [[MIDDLE_ROW]]


def test_a_row_that_keeps_failing_fails_the_load(monkeypatch):
    """Documents that never landed must fail the load, not shorten it: a load
    that returns zero having dropped rows is a truncated series reported as a
    fast one."""
    driver = FlakyDriver({MIDDLE_ROW}, forever=True)
    with pytest.raises(load_retry.RetriesExhausted) as raised:
        scylla_batch(monkeypatch, driver)
    assert "ConnectionBusy" in str(raised.value)
    assert len(driver.attempts) == load_retry.DEFAULT_POLICY.attempts


def test_a_retry_is_disclosed_by_the_loader(monkeypatch):
    """A retry is a condition of the run. Reported, a slightly slower number is
    explainable; silent, it is a mystery attributed to the engine."""
    tally = scylla_batch(monkeypatch, FlakyDriver({MIDDLE_ROW}))
    assert tally.summary() == {"retried_items": 1, "retries": 1}
    assert "overloaded" in tally.line()


def test_a_clean_load_says_it_had_no_retries(monkeypatch):
    tally = scylla_batch(monkeypatch, FlakyDriver(set()))
    assert tally.summary() == {"retried_items": 0, "retries": 0}
    assert tally.line() == "no retries"


class FlakyBulk:
    """A _bulk endpoint that raises for its first `failures` calls."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    async def __call__(self, pool, payload) -> None:
        self.calls += 1
        if self.remaining:
            self.remaining -= 1
            raise RuntimeError("bulk request had item failures, first: es_rejected")


def opensearch_bulk(monkeypatch, bulk: FlakyBulk) -> load_retry.RetryTally:
    monkeypatch.setattr(opensearch_load, "send_bulk", bulk)
    tally = load_retry.RetryTally()
    asyncio.run(opensearch_load.send(None, b"{}\n", tally))
    return tally


def test_a_bulk_request_that_fails_once_is_resent(monkeypatch):
    bulk = FlakyBulk(failures=1)
    tally = opensearch_bulk(monkeypatch, bulk)
    assert bulk.calls == 2
    assert tally.summary() == {"retried_items": 1, "retries": 1}


def test_a_bulk_request_that_keeps_failing_fails_the_load(monkeypatch):
    bulk = FlakyBulk(failures=99)
    with pytest.raises(load_retry.RetriesExhausted):
        opensearch_bulk(monkeypatch, bulk)
    assert bulk.calls == load_retry.DEFAULT_POLICY.attempts


def test_both_loaders_retry_under_the_same_policy(monkeypatch):
    """COMPARABILITY.md asks the two ingest paths to differ in what the engines
    do, not in how hard the client tries. Two policies that happen to agree
    today are not that commitment."""
    policies = []
    real = load_retry.send_with_retries_async

    async def recorder(items, send, tally, policy=load_retry.DEFAULT_POLICY,
                       **kwargs):
        policies.append(policy)
        return await real(items, send, tally, policy, **kwargs)

    monkeypatch.setattr(load_retry, "send_with_retries_async", recorder)
    scylla_batch(monkeypatch, FlakyDriver(set()))
    opensearch_bulk(monkeypatch, FlakyBulk(failures=0))
    assert policies == [load_retry.DEFAULT_POLICY, load_retry.DEFAULT_POLICY]


def test_the_retry_budget_is_recorded_in_the_run_header():
    """A run whose header does not state the retry budget cannot be compared
    with one taken under a different budget.

    Asserted once, on the shared driver, because both loaders now build their
    header there — which is the stronger property: the budget cannot be stated
    for one engine and omitted for the other."""
    source = (Path(__file__).resolve().parent.parent / "ftsbench" /
              "load_driver.py").read_text()
    assert "retry_attempts=load_retry.DEFAULT_POLICY.attempts" in source


@pytest.mark.parametrize("producer", write_path.WRITE_PATH_PRODUCERS)
def test_no_write_path_producer_builds_its_own_header(producer):
    """The header is the artifact's provenance. A hand-built header is how
    `concurrency` came to mean different things per engine without either side
    saying so — and how the churn artifacts on disk came to record no
    concurrency at all, leaving that defect unauditable from the files."""
    source = write_path.producer_source(producer)
    assert "runmeta.header(" not in source, \
        f"{producer} builds its own header instead of using load_driver"
