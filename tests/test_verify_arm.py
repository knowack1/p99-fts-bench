"""The gate that catches an arm running under another arm's knobs.

The failure it exists for is silent: a variable the image ignores, or an arm
inheriting `.env.sut`'s 376 into the ladder whose whole claim is that the buffer
is unset, both produce a complete and plausible curve under the wrong label.
"""
import pytest

from ftsbench import target, verify_arm

BUF376_LOG = """
2026-09-08T10:00:00Z INFO fts: ingest tuning for wiki.articles_body_fts: \
commit_interval=3s commit_threshold=disabled add_lock=exclusive \
dispatch=worker-pool metrics_interval=Some(1s)
2026-09-08T10:00:00Z INFO fts: index writer using 4 tantivy worker threads, \
376 MB buffer per thread, 4 merge threads (tokio workers=4, available_parallelism=4)
"""

BUF15_LOG = BUF376_LOG.replace("376 MB buffer", "15 MB buffer")
COMMIT30_LOG = BUF376_LOG.replace("commit_interval=3s", "commit_interval=30s")


def arm(config):
    return target.by_config(config)


def test_a_matching_log_passes():
    assert verify_arm.disagreements(arm("scylla-cdc-buf376"), BUF376_LOG) == []


def test_the_stock_floor_arm_expects_tantivys_own_minimum():
    """buf15 sets no variable at all, so the expectation cannot come from the
    env; it is tantivy's 15 MB floor, which is what 'unset' means here."""
    assert verify_arm.disagreements(arm("scylla-cdc-buf15"), BUF15_LOG) == []


def test_an_inherited_buffer_is_caught():
    """The exact accident: buf15 running with .env.sut's 376 still in the
    environment. Same label, same artifacts, different measurement."""
    problems = verify_arm.disagreements(arm("scylla-cdc-buf15"), BUF376_LOG)
    assert problems and "buffer_mb" in problems[0]


def test_a_stale_commit_interval_is_caught():
    problems = verify_arm.disagreements(arm("scylla-cdc-buf376-commit30"),
                                        BUF376_LOG)
    assert problems and "commit_interval" in problems[0]


def test_the_commit30_arm_passes_on_its_own_log():
    assert verify_arm.disagreements(arm("scylla-cdc-buf376-commit30"),
                                    COMMIT30_LOG) == []


def test_a_re_enabled_threshold_is_caught():
    """The threshold is disabled on every ScyllaDB arm; an image that ignored
    the variable would report the compiled-in 10,000 instead."""
    log = BUF376_LOG.replace("commit_threshold=disabled",
                             "commit_threshold=10000")
    problems = verify_arm.disagreements(arm("scylla-cdc-buf376"), log)
    assert problems and "commit_threshold" in problems[0]


def test_an_image_that_ignores_the_knobs_is_not_read_as_agreement():
    """Public 1.10.0 accepts every VS_FTS_* and states no tuning. Silence must
    fail, not pass — this is the whole reason the gate exists."""
    problems = verify_arm.disagreements(arm("scylla-cdc-buf376"), "")
    assert problems and "never stated" in problems[0]


def test_only_the_last_statement_counts():
    """The index is recreated per point, so an arm's log holds several
    statements; the point at hand is described by the most recent one."""
    log = BUF15_LOG + BUF376_LOG
    assert verify_arm.disagreements(arm("scylla-cdc-buf376"), log) == []


def test_the_worker_count_is_reported_rather_than_assumed():
    """376 was derived as a total budget over four workers. If the count is not
    four the total is not 1.5 GB and the parity argument needs rederiving."""
    assert verify_arm.worker_note(BUF376_LOG) == (
        "4 worker threads x 376 MB = 1504 MB total writer budget")


def test_a_different_worker_count_changes_the_reported_total():
    log = BUF376_LOG.replace("using 4 tantivy", "using 8 tantivy")
    assert "8 worker threads x 376 MB = 3008 MB" in verify_arm.worker_note(log)
