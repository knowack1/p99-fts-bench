"""The C6 artifact must be the six classes of one repetition and nothing else.

Two failure modes are worth a test each because both produce a chart that draws
without complaint:

- a glob that swept `c5-opensearch-*` into the plain-OpenSearch artifact would
  fold the refresh=30s configuration into it, and C6 would compare a
  configuration against a mixture containing itself;
- a missing class file would be drawn by `plot_c6` as a labelled empty slot,
  which reads as "the engine returned nothing for phrases".
"""
import json

import pytest

from ftsbench import c6_merge
from ftsbench.plotlib import QUERY_CLASSES

CONFIG = "opensearch"
REP = "1"
OTHER_CONFIG = "opensearch-refresh30"


def header(**overrides):
    return {"record": "header", "producer": "load_gen",
            "engine": "opensearch", "engine_version": "3.8.0",
            "corpus": "data/corpus.jsonl", "queries": "data/queries.json",
            "cache_state": "warm", "offered_qps": 100, "duration_s": 60,
            "warmup_s": 15, "concurrency": 16, "limit": 10, **overrides}


def operation(query_class, i, latency_ms=1.0):
    return {"record": "latency_op", "i": i, "op": "search",
            "class": query_class, "latency_ms": latency_ms, "ok": True}


def write_class_log(source, config, rep, query_class, **header_overrides):
    path = source / f"c5-{config}-{query_class}-{rep}.jsonl"
    records = [header(query_class=query_class, **header_overrides),
               operation(query_class, 0), operation(query_class, 1)]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def write_all_classes(source, config=CONFIG, rep=REP, **header_overrides):
    for query_class in QUERY_CLASSES:
        write_class_log(source, config, rep, query_class, **header_overrides)


def read_output(path):
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return lines[0], lines[1:]


def merged(tmp_path, config=CONFIG, rep=REP):
    output = tmp_path / f"c6-{config}-{rep}.jsonl"
    records = c6_merge.merge(tmp_path, config, rep)
    c6_merge.write(records, output)
    return read_output(output)


def test_every_class_ends_up_in_one_file(tmp_path):
    write_all_classes(tmp_path)
    _, operations = merged(tmp_path)
    assert len(operations) == 2 * len(QUERY_CLASSES)
    assert {record["class"] for record in operations} == set(QUERY_CLASSES)


def test_the_other_configuration_is_not_swept_in(tmp_path):
    """`c5-opensearch-*` also matches `c5-opensearch-refresh30-*`."""
    write_all_classes(tmp_path)
    write_all_classes(tmp_path, config=OTHER_CONFIG)
    _, operations = merged(tmp_path)
    assert len(operations) == 2 * len(QUERY_CLASSES), \
        "the refresh=30s logs were folded into the refresh=1s artifact"


def test_only_the_named_repetition_is_merged(tmp_path):
    write_all_classes(tmp_path, rep="1")
    write_all_classes(tmp_path, rep="2")
    _, operations = merged(tmp_path, rep="1")
    assert len(operations) == 2 * len(QUERY_CLASSES)


def test_a_missing_class_fails_instead_of_being_drawn_as_a_gap(tmp_path):
    write_all_classes(tmp_path)
    (tmp_path / f"c5-{CONFIG}-phrase-{REP}.jsonl").unlink()
    with pytest.raises(SystemExit) as raised:
        c6_merge.merge(tmp_path, CONFIG, REP)
    assert "phrase" in str(raised.value)


def test_a_class_log_with_no_operations_fails(tmp_path):
    write_all_classes(tmp_path)
    path = tmp_path / f"c5-{CONFIG}-bool_not-{REP}.jsonl"
    path.write_text(json.dumps(header(query_class="bool_not")) + "\n")
    with pytest.raises(SystemExit) as raised:
        c6_merge.merge(tmp_path, CONFIG, REP)
    assert "no records" in str(raised.value)


@pytest.mark.parametrize("field,value",
                         [("offered_qps", 200), ("corpus", "data/other.jsonl"),
                          ("engine_version", "3.7.0"), ("duration_s", 30),
                          ("concurrency", 32)])
def test_classes_that_measured_different_things_are_refused(tmp_path, field, value):
    """The six classes are one measurement taken a class at a time. If one of
    them was offered a different rate or read a different corpus, the per-class
    comparison C6 exists to draw is between two different runs."""
    write_all_classes(tmp_path)
    write_class_log(tmp_path, CONFIG, REP, "phrase", **{field: value})
    with pytest.raises(SystemExit) as raised:
        c6_merge.merge(tmp_path, CONFIG, REP)
    assert field in str(raised.value)


def test_the_class_a_log_was_taken_for_is_allowed_to_differ(tmp_path):
    """`query_class` is the one header field that must differ across the six."""
    write_all_classes(tmp_path)
    header_record, _ = merged(tmp_path)
    assert header_record["query_class"] == "all"


def test_the_header_names_what_it_was_built_from(tmp_path):
    write_all_classes(tmp_path)
    header_record, _ = merged(tmp_path)
    assert header_record["producer"] == c6_merge.PRODUCER
    assert header_record["query_classes"] == list(QUERY_CLASSES)
    assert header_record["merged_from"] == [
        f"c5-{CONFIG}-{name}-{REP}.jsonl" for name in QUERY_CLASSES]
    assert header_record["corpus"] == "data/corpus.jsonl", \
        "a merged artifact that drops its provenance cannot be reviewed later"
