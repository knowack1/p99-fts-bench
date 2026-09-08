"""The multi-process write path: budgets, the barrier, and aggregation.

Shape verified against VectorDBBench's `MultiProcessingSearchRunner` — spawn
context, every child checks in on a queue and waits on a condition, the parent
releases them and only then starts the clock. The tests here pin the parts that
would silently produce a wrong NUMBER rather than an error: a budget that loses
its remainder, a clock that includes client start-up, and a rate divided by the
wrong wall.
"""
import argparse

import pytest

from ftsbench import mp_load


def run_args(**overrides):
    defaults = dict(corpus="unused", batch_size=500, concurrency=64,
                    max_docs=1_000_000, target_rate=0.0, latency_log=None,
                    label="", cache_state="warm")
    return argparse.Namespace(**{**defaults, **overrides})


def test_the_concurrency_budget_is_split_across_workers_not_repeated():
    """--concurrency is the run's total offered load. Handing each worker the
    full value would offer workers x c and label it c — the ladder's x-axis
    would then be wrong by the worker count."""
    args = run_args(concurrency=64)
    shares = [mp_load.worker_args(args, i, 4).concurrency for i in range(4)]
    assert sum(shares) == 64
    assert shares == [16, 16, 16, 16]


def test_an_indivisible_concurrency_keeps_its_remainder():
    args = run_args(concurrency=10)
    shares = [mp_load.worker_args(args, i, 4).concurrency for i in range(4)]
    assert sum(shares) == 10
    assert shares == [3, 3, 2, 2]


def test_every_worker_offers_at_least_one_operation():
    """More workers than concurrency would otherwise give some workers zero,
    and a worker offering nothing still holds a shard — those documents would
    never be loaded and the point would fail its completeness gate."""
    args = run_args(concurrency=2)
    shares = [mp_load.worker_args(args, i, 8).concurrency for i in range(8)]
    assert min(shares) >= 1


def test_the_document_cap_is_split_across_workers():
    """Each worker capping at the whole run's max_docs would load
    workers x max_docs documents."""
    args = run_args(max_docs=1_000_000)
    caps = [mp_load.worker_args(args, i, 3).max_docs for i in range(3)]
    assert sum(caps) == 1_000_000


def test_an_uncapped_run_stays_uncapped_for_every_worker():
    args = run_args(max_docs=0)
    assert all(mp_load.worker_args(args, i, 4).max_docs == 0 for i in range(4))


def test_each_worker_carries_its_own_shard_identity():
    """With spawn a child inherits nothing, so the shard has to travel in the
    arguments or every worker would load shard 0."""
    args = run_args()
    identities = [(w.shard_index, w.shard_count)
                  for w in (mp_load.worker_args(args, i, 3) for i in range(3))]
    assert identities == [(0, 3), (1, 3), (2, 3)]


def test_worker_args_do_not_mutate_the_run_args():
    """The parent reuses its own args to build every worker's share; mutating
    in place would give worker 2 worker 1's already-divided budget."""
    args = run_args(concurrency=64, max_docs=1_000_000)
    mp_load.worker_args(args, 0, 4)
    assert (args.concurrency, args.max_docs) == (64, 1_000_000)


def test_throughput_uses_the_parents_wall_not_the_workers():
    """Workers finish at different times. Dividing by a worker's own wall, or
    summing per-worker rates, reports a rate the run never sustained."""
    results = [
        {"shard": 0, "concurrency": 8, "wall_s": 10.0, "ops": 1, "docs": 500,
         "ok_docs": 500, "errors": 0, "first_error": None, "retries": {}},
        {"shard": 1, "concurrency": 8, "wall_s": 5.0, "ops": 1, "docs": 500,
         "ok_docs": 500, "errors": 0, "first_error": None, "retries": {}},
    ]
    summary = mp_load.aggregate(results, wall_s=10.0)
    assert summary["docs_per_s"] == 100.0
    assert summary["concurrency"] == 16
    assert summary["workers"] == 2


def test_rejected_documents_are_not_counted_as_delivered_throughput():
    """docs includes what the engine refused; ok_docs is what landed. An engine
    that starts rejecting under merge pressure must not read as a fast one."""
    results = [{"shard": 0, "concurrency": 4, "wall_s": 2.0, "ops": 2,
                "docs": 1000, "ok_docs": 400, "errors": 1,
                "first_error": "rejected", "retries": {}}]
    summary = mp_load.aggregate(results, wall_s=2.0)
    assert summary["docs_per_s"] == 200.0
    assert summary["errors"] == 1


def test_a_worker_that_never_checks_in_fails_loudly():
    """A client that cannot connect would otherwise leave the parent blocked
    forever, which looks exactly like a slow engine."""
    class NeverFills:
        def qsize(self):
            return 0

    with pytest.raises(TimeoutError, match="checked in"):
        mp_load._await_check_in(NeverFills(), workers=2, timeout_s=0.1)


def test_check_in_returns_once_every_worker_is_ready():
    class FillsOnThirdLook:
        def __init__(self):
            self.looks = 0

        def qsize(self):
            self.looks += 1
            return 2 if self.looks >= 3 else 0

    queue = FillsOnThirdLook()
    mp_load._await_check_in(queue, workers=2, timeout_s=5.0)
    assert queue.looks >= 3


def test_both_engines_have_a_picklable_worker_entrypoint():
    """spawn pickles the callable by qualified name, so a closure or a bound
    method would fail at submit time rather than in review."""
    for engine, entrypoint in mp_load.WORKERS.items():
        assert entrypoint.__module__ == "ftsbench.mp_load", engine
        assert getattr(mp_load, entrypoint.__name__, None) is entrypoint
