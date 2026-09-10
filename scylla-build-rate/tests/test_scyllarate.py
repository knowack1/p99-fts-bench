import argparse
import json
import uuid

import pytest

from scyllarate import cli, corpus, report, session, sweep

PAGE_ID = 193002
PAGE_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wikipedia-page:{PAGE_ID}"))


def a_document(page_id: int = PAGE_ID) -> dict:
    return {"id": page_id,
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"wikipedia-page:{page_id}")),
            "title": "Washington (footballer)",
            "text": "Washington is a Brazilian football player."}


def write_corpus(tmp_path, documents: list[dict]) -> str:
    path = tmp_path / "corpus.jsonl"
    path.write_text("".join(json.dumps(doc) + "\n" for doc in documents))
    return str(path)


def a_point(concurrency: int = 8, errors: int = 0) -> report.PointResult:
    return report.PointResult(concurrency=concurrency, docs=100, errors=errors,
                              wall_s=2.0, docs_per_s=50.0, p50_ms=1.5, p99_ms=9.0)


def test_parses_a_list_of_concurrency_levels():
    assert cli._concurrency_list("8,16,32") == [8, 16, 32]


def test_keeps_a_repeated_level_so_a_warm_up_point_survives():
    assert cli._concurrency_list("8,8,16") == [8, 8, 16]


def test_tolerates_whitespace_and_trailing_separators():
    assert cli._concurrency_list(" 8 , 16 ,") == [8, 16]


def test_rejects_a_non_integer_level():
    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        cli._concurrency_list("8,many")


def test_rejects_a_level_below_one():
    with pytest.raises(argparse.ArgumentTypeError, match="must be >= 1"):
        cli._concurrency_list("8,0")


def test_rejects_an_empty_concurrency_list():
    with pytest.raises(argparse.ArgumentTypeError, match="at least one level"):
        cli._concurrency_list(",")


def test_splits_contact_points():
    assert cli._host_list("10.0.0.1, 10.0.0.2") == ["10.0.0.1", "10.0.0.2"]


def test_rejects_an_empty_host_list():
    with pytest.raises(argparse.ArgumentTypeError, match="at least one contact point"):
        cli._host_list(" ")


def test_resolves_a_consistency_level_by_name():
    assert cli._consistency("local_quorum") == session.consistency_from_name("LOCAL_QUORUM")


def test_rejects_an_unknown_consistency_level():
    with pytest.raises(argparse.ArgumentTypeError, match="unknown consistency"):
        cli._consistency("EVENTUALLY_MAYBE")


def test_maps_a_document_onto_the_articles_columns():
    assert corpus._to_insert_params(a_document()) == (
        uuid.UUID(PAGE_UUID), PAGE_ID,
        "Washington (footballer)", "Washington is a Brazilian football player.")


def test_reads_every_document_when_no_limit_is_given(tmp_path):
    path = write_corpus(tmp_path, [a_document(1), a_document(2), a_document(3)])
    assert len(list(corpus.read_insert_params(path))) == 3


def test_stops_at_the_document_limit(tmp_path):
    path = write_corpus(tmp_path, [a_document(1), a_document(2), a_document(3)])
    assert len(list(corpus.read_insert_params(path, max_docs=2))) == 2


def test_percentile_picks_the_nearest_rank():
    values = [float(n) for n in range(1, 101)]
    assert (report.percentile(values, 0.50), report.percentile(values, 0.99)) == (50.0, 99.0)


def test_percentile_of_a_single_sample_is_that_sample():
    assert report.percentile([7.0], 0.99) == 7.0


def test_percentile_of_nothing_is_zero():
    assert report.percentile([], 0.99) == 0.0


def a_topology() -> session.Topology:
    return session.Topology(scylla_version="2026.3.0-rc2", routing="TokenAwarePolicy",
                            compression="False", driver_version="3.29.11",
                            protocol_version="5", reactor="LibevConnection",
                            shard_aware="True",
                            shards="127.0.0.1:9042=shards:3,connected:3",
                            tablets="False")


def test_header_carries_the_topology_as_comment_lines():
    lines = report._header_lines(a_topology(), {"corpus": "data/corpus.jsonl"})
    assert all(line.startswith("# ") for line in lines)
    assert "# shard_aware=True" in lines
    assert "# corpus=data/corpus.jsonl" in lines


def test_csv_has_a_header_row_and_one_row_per_point():
    text = report.csv_text(a_topology(), {}, [a_point(8), a_point(16)])
    rows = [line for line in text.splitlines() if not line.startswith("#")]
    assert rows[0] == "concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms"
    assert len(rows) == 3


def test_csv_row_reports_the_two_plotted_metrics():
    row = report._csv_row(a_point(32)).split(",")
    assert (row[0], row[4], row[6]) == ("32", "50.0", "9.000")


def test_summarize_divides_delivered_documents_by_the_wall():
    counters = sweep.Counters()
    for latency in (1.0, 2.0, 3.0, 4.0):
        counters.record_ok(latency)
    result = sweep._summarize(4, counters, wall_s=2.0)
    assert (result.docs, result.docs_per_s, result.p99_ms) == (4, 2.0, 4.0)


def test_summarize_keeps_failures_out_of_the_rate_and_the_percentiles():
    counters = sweep.Counters()
    counters.record_ok(5.0)
    counters.record_error(RuntimeError("connection busy"))
    result = sweep._summarize(2, counters, wall_s=1.0)
    assert (result.docs, result.errors, result.docs_per_s) == (1, 1, 1.0)


def test_first_failure_is_remembered_for_the_operator():
    counters = sweep.Counters()
    counters.record_error(RuntimeError("connection busy"))
    counters.record_error(RuntimeError("something later"))
    assert counters.first_error == "RuntimeError: connection busy"


def test_a_sweep_with_any_failed_insert_exits_non_zero():
    from scyllarate.__main__ import _exit_code
    assert _exit_code([a_point(8), a_point(16, errors=3)]) == 1


def test_a_clean_sweep_exits_zero():
    from scyllarate.__main__ import _exit_code
    assert _exit_code([a_point(8), a_point(16)]) == 0
