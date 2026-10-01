"""The latency axis, and the four ways it would quietly draw the wrong chart.

A millisecond is only comparable to another millisecond that measured the same
thing. Two `latency_unit` values on one axis compare a 1,024-document request
against a one-document one; two `latency_basis` values compare a wait-included
latency against a service time; a rung whose `in_flight_peak` reached the cap
reports what the harness did; and a rung that spent its p99 queueing reports the
schedule rather than the engine. All four are pinned here.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

CHARTS_DIR = Path(__file__).resolve().parent.parent


def load_module(name: str):
    path = CHARTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHART = load_module("latency_vs_offered")

HEADER = ("concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms,"
          "batch_size,requests,failed_requests,index_docs,"
          "index_docs_per_s,index_lag_docs,index_settle_s,"
          "index_settled,index_status,engine,"
          "target_docs_per_s,achieved_offered_ratio,queue_p99_ms,"
          "in_flight_peak,generator_saturated")
PREAMBLE = ("# latency_unit=bulk_request\n"
            "# latency_basis=intended_start\n"
            "# corpus=test\n")


def a_row(offered, p50="200.0", p99="300.0", queue="0.1", peak=10,
          saturated="false", wall="120.000", engine="opensearch", batch=1024):
    return (f"128,1000,0,{wall},5000,{p50},{p99},{batch},10,0,"
            f"1000,4900,0,1.0,true,searchable,{engine},"
            f"{offered},1.0,{queue},{peak},{saturated}")


def write_points(path: Path, rows, preamble: str = PREAMBLE) -> Path:
    path.write_text(preamble + "\n".join([HEADER] + rows) + "\n")
    return path


def row_dict(line: str) -> dict:
    return dict(zip(HEADER.split(","), line.split(",")))


def collect(path: Path, metrics=CHART.METRICS):
    return CHART.rvc.collect_named("arm", str(path), True, metrics,
                                   contribute=CHART.points_of)


# --- y is the percentile the harness recorded, per request ----------------

def test_the_two_families_come_off_the_percentile_columns():
    row = row_dict(a_row(4096, p50="234.614", p99="517.973"))

    assert CHART.latency_of(row, CHART.P50) == 234.614
    assert CHART.latency_of(row, CHART.P99) == 517.973


def test_a_blank_percentile_is_dropped_rather_than_drawn_as_a_free_request():
    row = row_dict(a_row(4096, p50="", p99="517.973"))

    assert CHART.latency_of(row, CHART.P50) is None
    assert CHART.points_of(row, "arm", CHART.METRICS) == [
        ("arm", CHART.P99, 4096, 517.973, 120.0)]


def test_a_concurrency_ladder_csv_is_refused_rather_than_placed_at_an_invented_x(tmp_path):
    body = ("8,1000,0,10.000,5000,1.0,2.0,1,10,0,1000,4900,0,1.0,"
            "true,SERVING,scylladb,,,0.000,8,")
    path = write_points(tmp_path / "closed.csv", [body])

    with pytest.raises(SystemExit) as refused:
        collect(path)

    assert "closed-loop service times" in str(refused.value)


# --- a millisecond has to mean the same work on both lines ----------------

def test_two_latency_units_on_one_axis_are_refused(tmp_path):
    bulk = write_points(tmp_path / "bulk.csv", [a_row(4096)])
    inserts = write_points(tmp_path / "insert.csv", [a_row(4096)],
                           preamble="# latency_unit=insert_request\n")

    with pytest.raises(SystemExit) as refused:
        CHART.latency_unit([str(bulk), str(inserts)], allow_mixed=False)

    assert "bulk_request" in str(refused.value)
    assert "insert_request" in str(refused.value)


def test_mixed_units_are_drawn_only_when_the_caller_says_so(tmp_path):
    bulk = write_points(tmp_path / "bulk.csv", [a_row(4096)])
    inserts = write_points(tmp_path / "insert.csv", [a_row(4096)],
                           preamble="# latency_unit=insert_request\n")

    unit = CHART.latency_unit([str(bulk), str(inserts)], allow_mixed=True)

    assert unit == "bulk_request/insert_request"


def test_one_unit_reaches_the_axis_label(tmp_path):
    path = write_points(tmp_path / "one.csv", [a_row(4096)])

    assert CHART.latency_unit([str(path)], allow_mixed=False) == "bulk_request"


def test_two_latency_bases_are_refused_with_no_way_to_override(tmp_path):
    """`intended_start` counts the wait to be sent and `service` does not, so
    pooling them draws two different measurements as one line."""
    paced = write_points(tmp_path / "paced.csv", [a_row(4096)])
    closed = write_points(tmp_path / "service.csv", [a_row(4096)],
                          preamble="# latency_basis=service\n")

    with pytest.raises(SystemExit) as refused:
        CHART.latency_basis([str(paced), str(closed)])

    assert "intended_start" in str(refused.value)
    assert "service" in str(refused.value)


# --- the cap gate: saturated is a finding, saturated AT the cap is not -----

def test_a_saturated_rung_at_the_cap_is_void():
    marks = CHART.marks_of([("arm", 16384, True, 128, 46660.0, 59435.0)], cap=128)

    assert marks[("arm", 16384)]["void"] is True
    assert CHART.verdict_of(marks[("arm", 16384)]) == "void"


def test_a_saturated_rung_below_the_cap_is_a_finding_not_an_instrument_reading():
    marks = CHART.marks_of([("arm", 16384, True, 40, 10.0, 5000.0)], cap=128)

    assert marks[("arm", 16384)]["void"] is False
    assert CHART.verdict_of(marks[("arm", 16384)]) == "saturated"


def test_without_a_cap_nothing_is_called_void():
    marks = CHART.marks_of([("arm", 16384, True, 128, 46660.0, 59435.0)], cap=0)

    assert marks[("arm", 16384)]["void"] is False
    assert CHART.verdict_of(marks[("arm", 16384)]) == "saturated"


def test_the_worst_repetition_of_a_rung_wins_every_field():
    marks = CHART.marks_of([("arm", 16384, False, 40, 1.0, 1000.0),
                            ("arm", 16384, True, 128, 900.0, 1000.0)], cap=128)

    assert marks[("arm", 16384)]["saturated"] is True
    assert marks[("arm", 16384)]["peak"] == 128
    assert marks[("arm", 16384)]["share"] == pytest.approx(0.9)


# --- the schedule-held gate ----------------------------------------------

def test_the_queue_share_is_the_fraction_of_p99_spent_waiting_to_be_sent():
    assert CHART.queue_share(46660.964, 59435.884) == pytest.approx(0.785, abs=1e-3)


def test_a_row_with_no_p99_has_no_share_rather_than_a_division_by_zero():
    assert CHART.queue_share(0.0, 0.0) == 0.0


def test_the_flags_come_off_the_row_the_binary_wrote():
    row = row_dict(a_row(16384, p99="59435.884", queue="46660.964",
                         peak=128, saturated="true"))

    assert CHART.flags_of(row, "arm") == [
        ("arm", 16384, True, 128, 46660.964, 59435.884)]


def test_a_rung_over_the_threshold_is_named_with_its_share():
    marks = {("arm", 16384): {"saturated": True, "peak": 128, "void": True,
                              "queue_p99": 46660.0, "share": 0.7851}}

    assert CHART.queued_rungs(marks, 0.10) == [
        "arm offered=16384 (79% of p99, queue_p99=46660 ms)"]
    assert CHART.queued_rungs(marks, 0.90) == []


# --- the footer says what may not be quoted -------------------------------

def test_the_footer_names_the_void_rungs_and_the_cap_that_made_them_void():
    marks = {("arm", 16384): {"saturated": True, "peak": 128, "void": True,
                              "queue_p99": 46660.0, "share": 0.7851}}

    text = " ".join(CHART.footer_lines(marks, [], "bulk_request",
                                       "intended_start", 0.10, 128, True))

    assert "may not be quoted" in text
    assert "VOID (1): arm offered=16384 (in_flight_peak=128)" in text
    assert "79% of p99" in text


def test_the_footer_says_nothing_is_marked_void_when_no_cap_was_given():
    marks = {("arm", 16384): {"saturated": True, "peak": 128, "void": False,
                              "queue_p99": 0.1, "share": 0.0}}

    text = " ".join(CHART.footer_lines(marks, [], "bulk_request",
                                       "intended_start", 0.10, 0, True))

    assert "No --concurrency cap was given" in text
    assert "VOID" not in text


def test_the_footer_says_a_closed_loop_csv_cannot_show_a_backlog():
    text = " ".join(CHART.footer_lines({}, [], "insert_request", "service",
                                       0.10, 0, True))

    assert "cannot show a backlog" in text


def test_the_footer_never_calls_a_request_a_document():
    text = " ".join(CHART.footer_lines({}, [], "bulk_request",
                                       "intended_start", 0.10, 128, True))

    assert "a request is not a document" in text


# --- the table twin carries what makes a point readable -------------------

def test_the_table_twin_carries_the_queue_share_and_the_verdict():
    table = {"arm": {CHART.P99: {16384: {"reps": 1, "median": 59435.884,
                                         "min": 59435.884, "max": 59435.884,
                                         "shortest_wall_s": 180.087}}}}
    marks = {("arm", 16384): {"saturated": True, "peak": 128, "void": True,
                              "queue_p99": 46660.964, "share": 0.7851}}

    row = CHART.table_rows(table, ["arm"], marks)[0]

    assert row[:3] == ["arm", CHART.P99, 16384]
    assert row[-4:] == ["0.7851", "true", 128, "void"]


def test_the_table_columns_name_the_axis_and_the_gate_inputs():
    assert CHART.TABLE_COLUMNS[2] == "offered_docs_per_s"
    for column in ("latency_ms_median", "queue_p99_ms", "queue_share_of_p99",
                   "in_flight_peak", "verdict"):
        assert column in CHART.TABLE_COLUMNS


# --- percentiles are never pooled across repetitions ----------------------

def test_a_rung_reports_the_median_repetitions_own_percentile(tmp_path):
    path = write_points(tmp_path / "reps.csv",
                        [a_row(4096, p99="500.0"), a_row(4096, p99="900.0"),
                         a_row(4096, p99="700.0")])

    table = CHART.rvc.aggregate(collect(path, (CHART.P99,)))
    point = table["arm"][CHART.P99][4096]

    assert point["reps"] == 3
    assert point["median"] == 700.0, "a rep's own p99, not an average of three"
    assert (point["min"], point["max"]) == (500.0, 900.0)
