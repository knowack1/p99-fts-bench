import argparse
import io
import json
import random

import pytest

from ftsbench import freshness_probe
from ftsbench.engines import ScyllaEngine
from ftsbench.freshness_probe import (OpenSearchFreshness, PollOutcome,
                                      PollSettings, ProbeConfig, ProbeResult,
                                      ScyllaFreshness)

CORPUS_SAMPLE = (
    "Photosynthesis is the process used by plants to convert light energy into "
    "chemical energy. The running water of a river. Fresh water is fresh. "
    "FTS full text search freshness. Crassulacean acid metabolism."
)
FAST_POLL = PollSettings(interval_s=0.005, timeout_s=0.2)


class FakeTarget:
    """Becomes searchable on the `visible_on_poll`-th poll; never when None."""

    def __init__(self, visible_on_poll: int | None = 1) -> None:
        self.written: list[str] = []
        self.polls = 0
        self.closed = False
        self._visible_on_poll = visible_on_poll

    def write_marker(self, marker: str, i: int) -> str:
        self.written.append(marker)
        return f"key-{i}"

    def is_searchable(self, marker: str, doc_key: str) -> bool:
        self.polls += 1
        return (self._visible_on_poll is not None
                and self.polls >= self._visible_on_poll)

    def close(self) -> None:
        self.closed = True


class RaisingTarget(FakeTarget):
    def is_searchable(self, marker: str, doc_key: str) -> bool:
        self.polls += 1
        raise ConnectionError("503 index not SERVING")


class FakeResponse:
    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def put(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        return FakeResponse()

    def close(self) -> None:
        return None


def markers(count: int) -> list[str]:
    return [freshness_probe.new_marker(i) for i in range(count)]


def test_marker_cannot_occur_in_the_corpus():
    """The token must be impossible in Wikipedia text, or a lag of zero could be
    a corpus hit rather than the probe's own write."""
    for marker in markers(200):
        assert marker not in CORPUS_SAMPLE.lower()


def test_marker_always_carries_a_digit_so_it_can_be_no_english_word():
    for marker in markers(200):
        assert any(character.isdigit() for character in marker)


def test_marker_is_a_single_token_under_both_analyzers():
    """Only [a-z0-9]: punctuation splitting must not be able to break the marker
    into pieces that could match something else."""
    for marker in markers(200):
        assert marker.isalnum()
        assert marker == marker.lower()


def test_markers_do_not_repeat_across_probes_or_runs():
    """Unseeded on purpose: a repeated marker would let a leftover document from
    an earlier run answer this run's search before the write happened."""
    assert len(set(markers(500))) == 500
    assert freshness_probe.new_marker(0) != freshness_probe.new_marker(0)


def test_marker_carries_the_sentinel_prefix():
    assert freshness_probe.new_marker(7).startswith(freshness_probe.MARKER_PREFIX)


def test_probe_documents_use_negative_page_ids():
    assert freshness_probe.probe_page_id(0) < 0
    assert freshness_probe.probe_document("ftsfresh0abc", 3)["body"] == "ftsfresh0abc"


def test_a_timed_out_probe_is_recorded_not_dropped():
    """A write the engine accepted and will not serve is the finding."""
    target = FakeTarget(visible_on_poll=None)
    result = freshness_probe.run_one_probe(target, i=0, poll=FAST_POLL)
    record = freshness_probe.build_record(result, probe_config())
    assert record["timed_out"] is True
    assert record["t_searchable_s"] is None
    assert record["lag_s"] is None
    assert record["polls"] > 1


def test_a_successful_probe_records_a_lag_and_is_not_flagged():
    target = FakeTarget(visible_on_poll=3)
    result = freshness_probe.run_one_probe(target, i=0, poll=FAST_POLL)
    record = freshness_probe.build_record(result, probe_config())
    assert record["timed_out"] is False
    assert record["lag_s"] > 0
    assert record["polls"] == 3


def test_poll_interval_is_recorded_because_it_bounds_the_resolution():
    record = freshness_probe.build_record(
        successful_result(), probe_config(poll_interval_s=0.05))
    assert record["poll_interval_s"] == 0.05


def test_poll_errors_do_not_abort_the_probe_and_are_counted():
    """A 503 from a vector-store that has not reached SERVING is "not yet", but
    a probe that timed out because every poll failed must stay distinguishable
    from one where the write simply never appeared."""
    target = RaisingTarget(visible_on_poll=None)
    result = freshness_probe.run_one_probe(target, i=0, poll=FAST_POLL)
    record = freshness_probe.build_record(result, probe_config())
    assert record["timed_out"] is True
    assert record["poll_errors"] == record["polls"] > 0
    assert "503 index not SERVING" in record["last_poll_error"]


def test_searchable_or_error_reports_the_failure_rather_than_raising():
    found, error = freshness_probe.searchable_or_error(
        RaisingTarget(), "ftsfresh0abc", "key-0")
    assert found is False
    assert error.startswith("ConnectionError")


def test_lag_is_measured_from_the_write_acknowledgement():
    result = ProbeResult(i=0, marker="ftsfresh0abc", doc_key="key-0",
                         t_write_s=100.0, write_ms=12.5,
                         outcome=PollOutcome(101.25, polls=5, errors=0,
                                             last_error=None))
    record = freshness_probe.build_record(result, probe_config(origin_s=99.0))
    assert record["t_write_s"] == pytest.approx(1.0)
    assert record["t_searchable_s"] == pytest.approx(2.25)
    assert record["lag_s"] == pytest.approx(1.25)
    assert record["write_ms"] == pytest.approx(12.5)


def test_record_carries_the_engine_and_configuration_label():
    record = freshness_probe.build_record(
        successful_result(), probe_config(engine="scylladb", refresh_interval="cdc"))
    assert record["record"] == "freshness_probe"
    assert record["engine"] == "scylladb"
    assert record["refresh_interval"] == "cdc"


def test_every_probe_including_timeouts_reaches_the_artifact():
    target = FakeTarget(visible_on_poll=None)
    stream = io.StringIO()
    records = freshness_probe.run_probes(stream, probe_args(reps=3), target,
                                         probe_config())
    written = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == 3
    assert [record["timed_out"] for record in written] == [True, True, True]
    assert [record["i"] for record in written] == [0, 1, 2]


def test_settle_gap_is_jittered_so_probes_do_not_phase_lock():
    """A constant gap lands every write at the same phase of a fixed refresh
    cycle, making C8's range an artifact of the probe's own period."""
    rng = random.Random(1)
    gaps = {freshness_probe.settle_gap_s(2.0, 1.0, rng) for _ in range(20)}
    assert len(gaps) == 20
    assert all(2.0 <= gap <= 3.0 for gap in gaps)


def test_settle_jitter_is_reproducible_from_the_seed():
    first = [freshness_probe.settle_gap_s(2.0, 1.0, random.Random(7))
             for _ in range(3)]
    second = [freshness_probe.settle_gap_s(2.0, 1.0, random.Random(7))
              for _ in range(3)]
    assert first == second


def test_zero_jitter_leaves_the_gap_exact():
    assert freshness_probe.settle_gap_s(2.0, 0.0, random.Random(0)) == 2.0


def test_limit_must_obey_the_m1_rule():
    for limit in (0, -1, 1001):
        with pytest.raises(SystemExit):
            freshness_probe.require_m1_limit(limit)


def test_limit_of_one_thousand_is_allowed():
    freshness_probe.require_m1_limit(1000)


def test_refresh_label_uses_the_ingest_path_for_scylladb():
    args = argparse.Namespace(engine="scylladb", refresh_interval="1s", path="cdc")
    assert freshness_probe.refresh_label(args) == "cdc"


def test_refresh_label_uses_the_setting_for_opensearch():
    args = argparse.Namespace(engine="opensearch", refresh_interval="30s",
                              path="cdc")
    assert freshness_probe.refresh_label(args) == "30s"


def test_a_mislabelled_refresh_interval_is_warned_about(capsys):
    freshness_probe.warn_on_refresh_mismatch("30s", observed="1s")
    assert "WARNING" in capsys.readouterr().err


def test_a_matching_refresh_interval_is_silent(capsys):
    freshness_probe.warn_on_refresh_mismatch("30s", observed="30s")
    assert capsys.readouterr().err == ""


def test_opensearch_write_never_forces_a_refresh():
    """?refresh would make the write searchable by construction."""
    probe = OpenSearchFreshness("http://localhost:9200", "wiki-articles")
    probe._session = FakeSession()
    probe.write_marker("ftsfresh0abc", 0)
    url, kwargs = probe._session.calls[0]
    assert "refresh" not in url
    assert "params" not in kwargs
    assert kwargs["json"]["body"] == "ftsfresh0abc"


def test_opensearch_searchability_requires_the_written_document_id():
    probe = OpenSearchFreshness("http://localhost:9200", "wiki-articles")
    probe._engine = FakeSearchEngine(hits=["some-other-doc"])
    assert probe.is_searchable("ftsfresh0abc", "ftsfresh0abc") is False
    probe._engine = FakeSearchEngine(hits=["ftsfresh0abc"])
    assert probe.is_searchable("ftsfresh0abc", "ftsfresh0abc") is True


class FakeSearchEngine:
    def __init__(self, hits: list[str]) -> None:
        self._hits = hits

    def search(self, query_text: str, limit: int = 10) -> list[str]:
        return self._hits


def test_the_scylladb_probe_does_not_reimplement_the_m1_query():
    """The fixed M1 shape stays single-sourced in engines.ScyllaEngine; a second
    copy here would be free to drift out of parity with every other read tool."""
    assert ScyllaFreshness._build_query is ScyllaEngine._build_query
    assert ScyllaFreshness.search is ScyllaEngine.search


def test_the_scylladb_probe_query_keeps_the_fixed_m1_shape():
    query = build_probe_query("ftsfresh0abc", limit=10)
    assert query.count("WHERE") == 1
    assert "WHERE BM25(body, 'ftsfresh0abc') > 0" in query
    assert "ORDER BY BM25(body, 'ftsfresh0abc')" in query
    assert query.rstrip().endswith("LIMIT 10")


def build_probe_query(marker: str, limit: int) -> str:
    probe = object.__new__(ScyllaFreshness)
    probe._table, probe._column = "articles", "body"
    # _build_query reads the projection too; the real __init__ sets it via
    # ScyllaEngine, which this hand-built object skips.
    probe._projection = "article_id"
    return probe._build_query(marker, limit)


def test_summary_names_the_timeouts_rather_than_hiding_them(capsys):
    freshness_probe.print_summary([
        {"timed_out": False, "lag_s": 1.0},
        {"timed_out": True, "lag_s": None},
        {"timed_out": False, "lag_s": 3.0},
    ])
    err = capsys.readouterr().err
    assert "median=2.000s" in err
    assert "timeouts=1" in err


def test_summary_of_an_all_timeout_run_says_so(capsys):
    freshness_probe.print_summary([{"timed_out": True, "lag_s": None}])
    assert "timed out" in capsys.readouterr().err


def probe_config(engine: str = "opensearch", refresh_interval: str = "1s",
                 poll_interval_s: float = 0.005,
                 origin_s: float = 0.0) -> ProbeConfig:
    return ProbeConfig(engine=engine, refresh_interval=refresh_interval,
                       poll_interval_s=poll_interval_s, origin_s=origin_s)


def probe_args(reps: int) -> argparse.Namespace:
    return argparse.Namespace(reps=reps, poll_interval=FAST_POLL.interval_s,
                              timeout=FAST_POLL.timeout_s, settle=0.0,
                              jitter=0.0, seed=1)


def successful_result() -> ProbeResult:
    return ProbeResult(i=0, marker="ftsfresh0abc", doc_key="key-0",
                       t_write_s=1.0, write_ms=2.0,
                       outcome=PollOutcome(2.0, polls=4, errors=0,
                                           last_error=None))
