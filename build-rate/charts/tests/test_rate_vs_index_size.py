"""Two engines on one axis, and the merge that makes them look like one.

Both halves write `c64-1.csv` into their own samples directory. Globbed onto one
chart without an engine on the series name, the ScyllaDB build and the
OpenSearch one at the same concurrency group together and are drawn as two
repetitions of a single line — a bold pointwise median of two engines, which is
a number nothing measured. That is what these pin, along with the rule that a
single-engine run must keep reading the way `tools/plot_build_growth.py` reads.
"""
import importlib.util
import sys
from pathlib import Path

CHARTS_DIR = Path(__file__).resolve().parent.parent

SAMPLE_HEADER = ("level,concurrency,t_s,docs_submitted,submit_docs_per_s,"
                 "docs_indexed,index_docs_per_s,index_status,"
                 "docs_accepted,accepted_docs_per_s")
READINGS = [(0.5, 5000, 5000), (1.0, 10000, 10000), (1.5, 15000, 15000)]


def load_module(name: str):
    """Not a package module — charts/ is a script directory, and the scripts put
    what they need on sys.path themselves the way tools/ scripts do."""
    path = CHARTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHART = load_module("rate_vs_index_size")


def write_series(directory: Path, name: str, readings=READINGS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body = [SAMPLE_HEADER]
    body += [f"1,64,{t},{submitted},0,{indexed},0,SERVING"
             for t, submitted, indexed in readings]
    path.write_text("# scylla_version=test\n" + "\n".join(body) + "\n")
    return path


def test_a_build_without_an_engine_is_named_the_way_it_always_was():
    """`tools/plot_build_growth.py` charts one engine at a time, and a prefix
    on every line there would be noise rather than identity."""
    assert CHART.name_of("", 8, 0) == "c=8"
    assert CHART.name_of("", 8, 512) == "c=8 batch=512"


def test_an_engine_that_was_named_rides_at_the_front_of_the_series():
    assert CHART.name_of("scylladb", 64, 0) == "scylladb c=64"
    assert CHART.name_of("opensearch", 64, 512) == "opensearch c=64 batch=512"


def test_the_same_level_on_both_engines_is_two_series(tmp_path):
    """Without the engine these two group together and the chart draws their
    pointwise median as if it were one build measured twice."""
    write_series(tmp_path / "scylla", "c64-1.csv")
    write_series(tmp_path / "opensearch", "c64-b1-1.csv")

    levels = (CHART.load(str(tmp_path / "scylla" / "c*.csv"), CHART.SCYLLA_ENGINE)[0]
              + CHART.load(str(tmp_path / "opensearch" / "c*.csv"),
                           CHART.OPENSEARCH_ENGINE)[0])
    grouped = CHART.by_series(levels)

    assert sorted(grouped) == ["opensearch c=64 batch=1", "scylladb c=64"]
    assert all(len(reps) == 1 for reps in grouped.values())


def test_repetitions_of_one_build_stay_one_series(tmp_path):
    """The thing the engine prefix must not break: N reps of a build are still
    thin lines behind one bold median."""
    write_series(tmp_path / "rep1", "c64-b512-1.csv")
    write_series(tmp_path / "rep2", "c64-b512-2.csv")

    levels = (CHART.load(str(tmp_path / "rep1" / "c*.csv"), CHART.OPENSEARCH_ENGINE)[0]
              + CHART.load(str(tmp_path / "rep2" / "c*.csv"),
                           CHART.OPENSEARCH_ENGINE)[0])
    grouped = CHART.by_series(levels)

    assert list(grouped) == ["opensearch c=64 batch=512"]
    assert len(grouped["opensearch c=64 batch=512"]) == 2


def test_scylladb_leads_the_order_so_it_keeps_the_pinned_colour(tmp_path):
    """A reader carries colours between the run's two charts; ScyllaDB is the
    blue on both or it is the blue on neither."""
    write_series(tmp_path / "scylla", "c64-1.csv")
    write_series(tmp_path / "os1024", "c64-b1024-1.csv")
    write_series(tmp_path / "os128", "c64-b128-1.csv")

    levels = CHART.load(str(tmp_path / "scylla" / "c*.csv"), CHART.SCYLLA_ENGINE)[0]
    for name in ("os1024", "os128"):
        levels += CHART.load(str(tmp_path / name / "c*.csv"),
                             CHART.OPENSEARCH_ENGINE)[0]
    grouped = CHART.by_series(levels)
    order = CHART.series_order(grouped)

    assert order == ["scylladb c=64", "opensearch c=64 batch=128",
                     "opensearch c=64 batch=1024"]
    assert CHART.colour_for(grouped, order)[0] == CHART.SCYLLA_COLOR


def test_a_build_nobody_watched_is_skipped_by_name_rather_than_drawn(tmp_path):
    """`--index-watch` is off by default on osrate; a whole arm can arrive with
    blank index columns, and a flat line at zero would read as an engine that
    indexed nothing."""
    directory = tmp_path / "opensearch"
    directory.mkdir()
    (directory / "c64-b512-1.csv").write_text(
        "# corpus=test\n" + SAMPLE_HEADER
        + "\n1,64,1.0,100,100.0,,,\n1,64,2.0,200,100.0,,,\n")

    levels, skipped = CHART.load(str(directory / "c*.csv"),
                                 CHART.OPENSEARCH_ENGINE)

    assert levels == []
    assert "c64-b512-1.csv" in skipped[0]
    assert "no index readings" in skipped[0]


def test_a_build_too_short_to_have_a_shape_is_skipped_by_name(tmp_path):
    levels, skipped = CHART.load(
        str(write_series(tmp_path / "s", "c64-1.csv",
                         [(0.5, 5000, 5000)]).parent / "c*.csv"),
        CHART.SCYLLA_ENGINE)

    assert levels == []
    assert "readings" in skipped[0]


def test_the_grid_and_the_riser_floor_are_the_ones_the_sibling_chart_uses(tmp_path):
    """Reused, not restated: a refresh-stepped OpenSearch series must widen the
    bucket here exactly as it does on tools/plot_build_growth.py."""
    write_series(tmp_path / "os", "c64-b512-1.csv",
                 [(1.0, 900, 0), (2.0, 1800, 900), (3.0, 2700, 900),
                  (4.0, 3600, 1800)])

    levels, _ = CHART.load(str(tmp_path / "os" / "c*.csv"),
                           CHART.OPENSEARCH_ENGINE)

    assert CHART.growth.riser_floor(levels) == 900
    assert CHART.growth.chosen_step(levels, 10) == 900


def test_the_footer_warns_about_the_y_axis_only_when_both_engines_are_on_it():
    both = " ".join(CHART.footer_lines(1000, [], False, both_engines=True))
    alone = " ".join(CHART.footer_lines(1000, [], False, both_engines=False))

    assert "y does NOT" in both
    assert "y does NOT" not in alone
    assert "NOT QUOTABLE" in alone
