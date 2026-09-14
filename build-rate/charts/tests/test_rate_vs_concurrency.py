"""Ten lines off five configurations, and the two ways that silently becomes five.

The chart draws every engine-and-batch configuration twice — solid for what the
client submitted, dashed for what the engine indexed. Two failures would leave a
plausible-looking chart rather than an error: the indexed family keying on its
own series name, which costs it the shared colour and turns a pair into two
unrelated lines; and a blank `index_docs_per_s` read as zero, which draws an
engine that indexed nothing.
"""
import importlib.util
import sys
from pathlib import Path

CHARTS_DIR = Path(__file__).resolve().parent.parent


def load_module(name: str):
    """Not a package module — charts/ is a script directory, and the scripts put
    what they need on sys.path themselves the way tools/ scripts do."""
    path = CHARTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHART = load_module("rate_vs_concurrency")

POINT_HEADER = ("concurrency,docs,errors,wall_s,docs_per_s,p50_ms,p99_ms,"
                "batch_size,requests,failed_requests,index_docs,"
                "index_docs_per_s,index_lag_docs,index_settle_s,"
                "index_settled,index_status,engine")


def write_points(path: Path, rows, wall="10.000") -> Path:
    """`rows` is (concurrency, docs_per_s, index_docs_per_s, engine, batch);
    an index rate of "" is a level that ran with the index unwatched."""
    body = [POINT_HEADER]
    body += [f"{conc},1000,0,{wall},{submitted},1.0,2.0,{batch},10,0,"
             f"1000,{indexed},0,1.0,true,SERVING,{engine}"
             for conc, submitted, indexed, engine, batch in rows]
    path.write_text("# corpus=test\n" + "\n".join(body) + "\n")
    return path


def test_submitted_only_collects_what_the_null_sink_chart_collects(tmp_path):
    """`--submitted-only` has to reproduce tools/plot_harness_grid.py on CSVs
    that carry the index columns, or there is no way to draw the one without
    the other."""
    csv_path = write_points(tmp_path / "os.csv",
                            [(8, 1000.0, 900.0, "opensearch", 512),
                             (16, 2000.0, 1800.0, "opensearch", 512)])

    points = CHART.collect(str(csv_path), True, CHART.OPENSEARCH_ENGINE,
                           (CHART.SUBMITTED,))

    assert {metric for _, metric, _, _, _ in points} == {CHART.SUBMITTED}
    assert len(points) == 2


def test_the_indexed_rate_rides_on_its_config_rather_than_a_new_one(tmp_path):
    """Solid and dashed are one configuration seen twice, so they key on the
    same series name — that is what lets the chart give them one colour."""
    csv_path = write_points(tmp_path / "os.csv",
                            [(8, 1000.0, 900.0, "opensearch", 512)])

    table = CHART.aggregate(CHART.collect(str(csv_path), True,
                                          CHART.OPENSEARCH_ENGINE, CHART.METRICS))

    assert list(table) == ["osrate batch=512"]
    assert CHART.metric_order(table["osrate batch=512"]) == [CHART.SUBMITTED,
                                                             CHART.INDEXED]
    assert table["osrate batch=512"][CHART.INDEXED][8]["median"] == 900.0


def test_an_unwatched_level_contributes_no_indexed_point_rather_than_a_zero(tmp_path):
    """A blank cell is a measurement that did not happen. Read as zero it draws
    an engine that indexed nothing, which is the one thing it does not say."""
    csv_path = write_points(tmp_path / "scylla.csv",
                            [(8, 1000.0, "", "scylladb", 1)])

    points = CHART.collect(str(csv_path), True, CHART.SCYLLA_ENGINE,
                           CHART.METRICS)

    assert [metric for _, metric, _, _, _ in points] == [CHART.SUBMITTED]


def test_a_zero_index_rate_is_kept_because_it_was_measured():
    """The mirror of the rule above: an engine that really indexed nothing in
    the window reported 0, and dropping that would hide it."""
    assert CHART.rate_of({"index_docs_per_s": "0.0"}, CHART.INDEXED) == 0.0
    assert CHART.rate_of({"index_docs_per_s": ""}, CHART.INDEXED) is None


def test_both_engines_keep_both_of_their_lines(tmp_path):
    """The merge the engine column exists to prevent, now doubled: five
    configurations must produce ten lines, not five and not one."""
    scylla = write_points(tmp_path / "scylla.csv",
                          [(8, 500.0, 400.0, "scylladb", 1)])
    osrate = write_points(tmp_path / "os.csv",
                          [(8, 9000.0, 8000.0, "opensearch", 512)])

    points = (CHART.collect(str(scylla), True, CHART.SCYLLA_ENGINE, CHART.METRICS)
              + CHART.collect(str(osrate), True, CHART.OPENSEARCH_ENGINE,
                              CHART.METRICS))
    table = CHART.aggregate(points)

    assert CHART.config_order(table) == [CHART.SCYLLA_SERIES, "osrate batch=512"]
    assert CHART.counted_points(table) == 4


def test_scylladb_leads_the_order_so_it_keeps_the_pinned_colour(tmp_path):
    """A reader carries colours between the run's two charts; ScyllaDB is the
    blue on both or it is the blue on neither."""
    csv_path = write_points(tmp_path / "mixed.csv",
                            [(8, 9000.0, 8000.0, "opensearch", 1024),
                             (8, 500.0, 400.0, "scylladb", 1),
                             (8, 9000.0, 8000.0, "opensearch", 128)])

    points = CHART.collect(str(csv_path), True, CHART.OPENSEARCH_ENGINE,
                           CHART.METRICS)
    order = CHART.config_order(CHART.aggregate(points))

    assert order == [CHART.SCYLLA_SERIES, "osrate batch=128", "osrate batch=1024"]


def test_the_table_says_which_rate_each_row_is(tmp_path):
    """Two rows per point differing only in a number are unreadable; the metric
    column is what makes the twin table usable at all."""
    csv_path = write_points(tmp_path / "os.csv",
                            [(8, 1000.0, 900.0, "opensearch", 512)])

    table = CHART.aggregate(CHART.collect(str(csv_path), True,
                                          CHART.OPENSEARCH_ENGINE, CHART.METRICS))
    rows = CHART.table_rows(table, CHART.config_order(table))

    assert CHART.TABLE_COLUMNS[1] == "metric"
    assert [row[1] for row in rows] == [CHART.SUBMITTED, CHART.INDEXED]
    assert [row[4] for row in rows] == ["1000.0", "900.0"]


def test_a_short_point_is_named_once_and_not_once_per_rate(tmp_path):
    """Wall time belongs to the level, not to the two rates taken off it."""
    csv_path = write_points(tmp_path / "os.csv",
                            [(8, 1000.0, 900.0, "opensearch", 512)],
                            wall="2.000")

    table = CHART.aggregate(CHART.collect(str(csv_path), True,
                                          CHART.OPENSEARCH_ENGINE, CHART.METRICS))

    assert len(CHART.short_points(table)) == 1


def test_the_warm_up_row_is_dropped_from_both_families(tmp_path):
    """The ladder repeats its first level as a throwaway. Kept in one family and
    dropped in the other, the pair would disagree about its own first point."""
    csv_path = write_points(tmp_path / "os.csv",
                            [(8, 10.0, 9.0, "opensearch", 512),
                             (8, 1000.0, 900.0, "opensearch", 512)])

    table = CHART.aggregate(CHART.collect(str(csv_path), False,
                                          CHART.OPENSEARCH_ENGINE, CHART.METRICS))

    assert table["osrate batch=512"][CHART.SUBMITTED][8]["median"] == 1000.0
    assert table["osrate batch=512"][CHART.INDEXED][8]["median"] == 900.0


def test_the_footer_says_what_dashed_means_only_when_dashed_is_drawn():
    table = {}
    with_indexed = " ".join(CHART.footer_lines(table, [], indexed=True))
    without = " ".join(CHART.footer_lines(table, [], indexed=False))

    assert "DASHED is index_docs_per_s" in with_indexed
    assert "DASHED" not in without


def test_the_footer_does_not_claim_a_warm_up_drop_that_did_not_happen():
    """`--keep-warmup` is how a four-rung ladder keeps its lowest point. Saying
    a throwaway row was dropped when every row was plotted would misdescribe the
    one point on the chart that was measured cold."""
    kept = " ".join(CHART.footer_lines({}, [], indexed=False, keep_warmup=True))
    dropped = " ".join(CHART.footer_lines({}, [], indexed=False, keep_warmup=False))

    assert "EVERY row is plotted" in kept
    assert "warm-up row is dropped" not in kept
    assert "warm-up row is dropped" in dropped
