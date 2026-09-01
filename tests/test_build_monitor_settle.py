"""The build is not over when the last document is written — it is over when the
last document can be found.

The campaign's first pass stopped the moment `docs_indexed` reached the corpus
count, which is one refresh interval too early. At refresh=1s that lost 22,554
of 270,269 documents from the searchable count; at refresh=30s the build
finished in 13 seconds and *no document was ever searchable* in the series. C2
read both as "never became fully searchable" and drew infinity, which is a
truthful reading of a series that threw the answer away.

The refresh=30s case is why the settle phase has to outrank the idle timeout:
once the loader stops, `docs_indexed` stops moving by definition, so the 30s
idle timeout and the 30s refresh interval are in a race that the idle timeout
usually wins.
"""
import argparse

import pytest

from ftsbench import build_monitor

TARGET = 270269


def args(**overrides):
    defaults = dict(until_docs=TARGET, idle_timeout=30.0, max_seconds=600.0,
                    startup_grace=600.0, settle_timeout=120.0)
    return argparse.Namespace(**{**defaults, **overrides})


def sample(indexed, searchable, status="n/a"):
    return {"docs_indexed": indexed, "docs_searchable": searchable,
            "index_status": status}


def stop(sample_record, *, idle_for=0.0, elapsed=10.0, settle_for=0.0,
         seen_progress=True, **overrides):
    return build_monitor.should_stop(
        args(**overrides), sample_record, sample_record["docs_indexed"],
        idle_for, elapsed, seen_progress, settle_for)


def test_the_run_continues_while_documents_are_still_becoming_searchable():
    assert not stop(sample(TARGET, 247715))


def test_the_run_ends_once_every_document_is_searchable():
    assert stop(sample(TARGET, TARGET))


def test_an_idle_indexed_count_is_expected_while_waiting_for_a_refresh():
    """After the loader stops, docs_indexed stops moving by construction. That
    is not a stall, and at refresh=30s treating it as one is what discarded the
    whole measurement."""
    assert not stop(sample(TARGET, 0), idle_for=31.0)


def test_waiting_for_searchability_is_bounded():
    """An index that never refreshes must not hold the campaign forever."""
    assert stop(sample(TARGET, 0), idle_for=31.0, settle_for=120.0)


def test_a_stall_before_the_target_still_ends_the_run():
    assert stop(sample(100_000, 90_000), idle_for=31.0)


def test_the_wall_clock_cap_outranks_the_settle_phase():
    assert stop(sample(TARGET, 0), elapsed=600.0, settle_for=1.0)


def test_scylladb_reports_one_count_so_the_settle_phase_is_a_no_op():
    """The vector-store status endpoint answers one `count`, which the sampler
    reports as both indexed and searchable."""
    assert stop(sample(TARGET, TARGET, status="SERVING"))


def test_scylladb_still_waits_for_the_index_to_be_published():
    assert not stop(sample(TARGET, TARGET, status="BUILDING"))


def test_an_engine_that_reports_no_searchable_count_is_not_waited_on():
    """A sampler without a searchable count cannot answer the question, and
    blocking on a field that will never appear would hang every run."""
    assert stop({"docs_indexed": TARGET, "index_status": "n/a"})


def test_no_target_means_the_idle_timeout_decides():
    assert stop(sample(TARGET, 0), until_docs=0, idle_for=31.0)


def test_the_settle_timeout_is_configurable_and_defaults_above_a_refresh():
    parsed = build_monitor.parse_args_from(
        ["--engine", "opensearch", "--output", "x.jsonl"])
    assert parsed.settle_timeout >= 30.0


@pytest.mark.parametrize("searchable", [0, 1, TARGET - 1])
def test_any_shortfall_keeps_the_run_open(searchable):
    assert not stop(sample(TARGET, searchable))


def test_the_series_records_how_long_it_was_willing_to_wait(tmp_path):
    """C2 is read off this series, so the series has to say what its own stop
    condition was: a bar reported as unresolved means something different if the
    monitor waited two seconds than if it waited two minutes."""
    import json

    class Sampler:
        def version(self):
            return "3.8.0"

    output = tmp_path / "series.jsonl"
    with output.open("w") as handle:
        build_monitor.write_header(
            handle, build_monitor.parse_args_from(
                ["--engine", "opensearch", "--output", str(output),
                 "--settle-timeout", "45"]), Sampler())
    header = json.loads(output.read_text().splitlines()[0])
    assert header["settle_timeout_s"] == 45.0
