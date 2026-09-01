"""SCHEMAS.md is the contract; these tests hold the producers to it.

`latency_op` is emitted by two harnesses that were built independently: the
ingest loaders write it through `LatencyLog` (C3) and the query load generator
writes it from its collector thread (C5, C6, C7). A field added, renamed or
dropped on one side alone would not fail anything — it would quietly change
what a percentile is computed from in half the charts. So the field list is
asserted against the document rather than against a copy of itself.
"""
import io
import json
import re
from pathlib import Path

from ftsbench import latency_log, load_gen, pacer
from ftsbench.load_gen import Outcome, Query

SCHEMAS_MD = Path(__file__).resolve().parent.parent / "SCHEMAS.md"


def documented_fields(record_name: str) -> tuple[str, ...]:
    """Pull the example object for a record out of SCHEMAS.md.

    Reading the document rather than restating it here is the point: a test that
    hardcodes the field list only proves the test agrees with itself.
    """
    text = SCHEMAS_MD.read_text(encoding="utf-8")
    heading = re.search(rf"^## `{re.escape(record_name)}`.*$", text,
                        flags=re.MULTILINE)
    assert heading, f"SCHEMAS.md has no section for `{record_name}`"
    block = re.search(r"```json\n(.*?)\n```", text[heading.end():],
                      flags=re.DOTALL)
    assert block, f"the `{record_name}` section has no JSON example"
    return tuple(json.loads(block.group(1)).keys())


def ingest_record() -> dict:
    """Round-tripped through the real stream rather than intercepted: the record
    a consumer reads is the serialized one, so that is what must conform."""
    stream = io.StringIO()
    log = latency_log.LatencyLog(stream=stream, origin_s=100.0)
    timing = latency_log.OpTiming(i=7, t_intended_s=100.5, t_start_s=100.6,
                                  t_end_s=100.7)
    log.record(timing, op="bulk", n_docs=500)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def search_record() -> dict:
    op = pacer.Op(i=7, t_intended_s=100.5)
    outcome = Outcome(t_start_s=100.6, t_end_s=100.7, hits=3, error=None)
    query = Query(text="kernel", query_class="rare_term", query_i=2)
    return load_gen.latency_op_record(op, query, outcome, origin_s=100.0)


def test_ingest_latency_op_matches_the_documented_field_list():
    assert tuple(ingest_record().keys()) == documented_fields("latency_op")


def test_search_latency_op_matches_the_documented_field_list():
    assert tuple(search_record().keys()) == documented_fields("latency_op")


def test_both_producers_emit_the_same_fields_in_the_same_order():
    assert tuple(ingest_record().keys()) == tuple(search_record().keys())


def test_the_two_producers_agree_on_the_latency_arithmetic():
    """Same intended, start and end times through both paths must give the same
    latency_ms, service_ms and queue_ms — otherwise C3 and C5 are measured on
    different definitions of the same word."""
    timed = ("latency_ms", "service_ms", "queue_ms")
    ingest, search = ingest_record(), search_record()
    assert [ingest[key] for key in timed] == [search[key] for key in timed]


def test_ingest_and_search_records_are_distinguishable_by_op():
    assert ingest_record()["op"] == "bulk"
    assert search_record()["op"] == load_gen.SEARCH_OP == "search"
