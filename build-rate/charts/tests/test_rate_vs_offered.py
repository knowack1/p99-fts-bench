"""The rate axis, and the three ways it would quietly draw the wrong chart.

A rate-ladder CSV has one concurrency for every rung — its cap — so rendering
it on the concurrency axis stacks every point on one x. A concurrency-ladder
CSV has no offered rate at all, so rendering it here would need one invented.
Both are refused. The third is a saturated rung drawn as though it kept up,
which is how a knee goes missing.
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


CHART = load_module("rate_vs_offered")
SIBLING = load_module("rate_vs_concurrency")

HEADER = ("concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms,"
          "batch_size,requests,failed_requests,index_docs,"
          "index_docs_per_s,index_lag_docs,index_settle_s,"
          "index_settled,index_status,engine,"
          "target_docs_per_s,achieved_offered_ratio,queue_p99_ms,"
          "in_flight_peak,generator_saturated")


def a_row(offered, submitted, indexed="", engine="scylladb", batch=1,
          saturated="false", peak=10, wall="10.000"):
    return (f"512,1000,0,{wall},{submitted},1.0,2.0,{batch},10,0,"
            f"1000,{indexed},0,1.0,true,SERVING,{engine},"
            f"{offered},1.0,0.5,{peak},{saturated}")


def write_points(path: Path, rows) -> Path:
    path.write_text("# corpus=test\n" + "\n".join([HEADER] + rows) + "\n")
    return path


def closed_loop_csv(path: Path) -> Path:
    """What the concurrency ladder writes: the rate columns blank, not zero."""
    body = ("8,1000,0,10.000,5000,1.0,2.0,1,10,0,1000,4900,0,1.0,"
            "true,SERVING,scylladb,,,0.000,8,")
    path.write_text("# corpus=test\n" + "\n".join([HEADER, body]) + "\n")
    return path


# --- x is the offered rate ------------------------------------------------

def test_the_x_value_is_what_the_client_was_told_to_send(tmp_path):
    row = dict(zip(HEADER.split(","), a_row(20000, 19900).split(",")))
    assert CHART.offered_of(row) == 20000


def test_a_row_with_no_offered_rate_has_no_x(tmp_path):
    row = dict(zip(HEADER.split(","), closed_loop_csv(tmp_path / "c.csv")
                   .read_text().splitlines()[2].split(",")))
    assert CHART.offered_of(row) is None


def test_both_engines_land_on_the_same_x_for_the_same_offered_rate(tmp_path):
    scylla = write_points(tmp_path / "s.csv", [a_row(50000, 49900, engine="scylladb")])
    opensearch = write_points(tmp_path / "o.csv",
                              [a_row(50000, 49800, engine="opensearch", batch=1024)])
    points = (CHART.rvc.collect_named("S", str(scylla), True, (CHART.SUBMITTED,),
                                      contribute=CHART.points_of)
              + CHART.rvc.collect_named("O", str(opensearch), True, (CHART.SUBMITTED,),
                                        contribute=CHART.points_of))

    assert {point[2] for point in points} == {50000}, "one offered rate, one x"


# --- the two refusals -----------------------------------------------------

def test_a_concurrency_ladder_csv_is_refused_rather_than_placed_at_an_invented_x(tmp_path):
    path = closed_loop_csv(tmp_path / "closed.csv")

    with pytest.raises(SystemExit) as refused:
        CHART.rvc.collect_named("arm", str(path), True, (CHART.SUBMITTED,),
                                contribute=CHART.points_of)

    assert "rate_vs_concurrency.py" in str(refused.value)


def test_a_rate_ladder_csv_is_refused_by_the_concurrency_chart(tmp_path):
    path = write_points(tmp_path / "paced.csv",
                        [a_row(20000, 19900), a_row(50000, 49900)])

    with pytest.raises(SystemExit) as refused:
        SIBLING.collect_named("arm", str(path), True, (SIBLING.SUBMITTED,))

    assert "rate_vs_offered.py" in str(refused.value)


def test_an_older_csv_without_the_rate_columns_still_renders_on_the_sibling(tmp_path):
    """The columns were appended, so a CSV written before them is a valid
    concurrency-ladder CSV and must not be mistaken for a paced one."""
    old_header = ",".join(HEADER.split(",")[:17])
    body = "8,1000,0,10.000,5000,1.0,2.0,1,10,0,1000,4900,0,1.0,true,SERVING,scylladb"
    path = tmp_path / "old.csv"
    path.write_text("# corpus=test\n" + "\n".join([old_header, body]) + "\n")

    points = SIBLING.collect_named("arm", str(path), True, (SIBLING.SUBMITTED,))

    assert [point[2] for point in points] == [8]


# --- saturation is a finding, not a gap -----------------------------------

def test_a_rung_any_rep_could_not_sustain_is_marked(tmp_path):
    marks = CHART.saturation_of([
        ("arm", 50000, False, 40),
        ("arm", 50000, True, 512),
    ])

    assert marks[("arm", 50000)]["saturated"] is True
    assert marks[("arm", 50000)]["peak"] == 512, "the worst rep's peak survives"


def test_a_rung_every_rep_sustained_is_not_marked():
    marks = CHART.saturation_of([("arm", 20000, False, 36),
                                 ("arm", 20000, False, 38)])

    assert marks[("arm", 20000)]["saturated"] is False


def test_the_flags_come_off_the_row_the_binary_wrote(tmp_path):
    row = dict(zip(HEADER.split(","),
                   a_row(50000, 30000, saturated="true", peak=512).split(",")))

    assert CHART.flags_of(row, "arm") == [("arm", 50000, True, 512)]


def test_the_footer_says_a_ringed_point_may_be_the_harness_not_the_engine():
    lines = CHART.footer_lines({("arm", 50000): {"saturated": True, "peak": 512}},
                               [], indexed=True, keep_warmup=True)
    text = " ".join(lines)

    assert "in_flight_peak" in text
    assert "HARNESS was the limit" in text
    assert "1 rung(s) saturated" in text


# --- the footer's short points name a rate, not a concurrency -------------

def test_a_short_point_is_named_by_its_offered_rate(tmp_path):
    table = {"arm": {CHART.SUBMITTED: {50000: {"shortest_wall_s": 1.2,
                                               "median": 1.0, "min": 1.0,
                                               "max": 1.0, "reps": 1}}}}

    assert CHART.short_points(table) == ["arm offered=50000 (1.2s)"]


# --- the table twin carries what makes a point readable -------------------

def test_the_table_twin_carries_saturation_and_the_in_flight_peak():
    table = {"arm": {CHART.SUBMITTED: {50000: {"reps": 1, "median": 30000.0,
                                               "min": 30000.0, "max": 30000.0,
                                               "shortest_wall_s": 4.0}}}}
    marks = {("arm", 50000): {"saturated": True, "peak": 512}}

    rows = CHART.table_rows(table, ["arm"], marks)

    assert rows[0][0] == "arm"
    assert rows[0][2] == 50000
    assert rows[0][-2] == "true"
    assert rows[0][-1] == 512


def test_the_table_columns_name_the_axis_as_an_offered_rate():
    assert CHART.TABLE_COLUMNS[2] == "offered_docs_per_s"
    assert "saturated" in CHART.TABLE_COLUMNS
    assert "in_flight_peak" in CHART.TABLE_COLUMNS
