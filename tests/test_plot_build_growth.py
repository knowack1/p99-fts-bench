"""The growth chart is only worth reading if every line on it survived the grid.

Two rules hold the whole chart up. A level starts at zero documents at zero
seconds — that point is real, it is what `t_s` is measured from, and dropping it
both inflates the first bucket and silently deletes any level whose first
reading already passed it. And the mark that says where the client stopped has
to come from the last index count that was actually read, because the reading
that closes the submit series carries none.
"""
import importlib.util
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))

SAMPLE_HEADER = ("level,concurrency,t_s,docs_submitted,submit_docs_per_s,"
                 "docs_indexed,index_docs_per_s,index_status,"
                 "docs_accepted,accepted_docs_per_s")


def load_plot_build_growth():
    """Not a package module — tools/ is a script directory, and the script puts
    BENCH_DIR on sys.path itself the way tools/co_check.py does."""
    path = BENCH_DIR / "tools" / "plot_build_growth.py"
    spec = importlib.util.spec_from_file_location("plot_build_growth", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


growth = load_plot_build_growth()


def rows(readings, closing=True):
    """`readings` is (t_s, docs_submitted, docs_indexed) per row; `closing` adds
    the blank-index row that ends every real submit series."""
    out = [{"level": "1", "concurrency": "8", "t_s": f"{t}",
            "docs_submitted": f"{submitted}", "submit_docs_per_s": "0",
            "docs_indexed": f"{indexed}", "index_docs_per_s": "0",
            "index_status": "SERVING"}
           for t, submitted, indexed in readings]
    if closing:
        last = readings[-1]
        out.append({"level": "1", "concurrency": "8", "t_s": f"{last[0]}",
                    "docs_submitted": f"{last[1]}", "submit_docs_per_s": "0",
                    "docs_indexed": "", "index_docs_per_s": "",
                    "index_status": ""})
    return out


def write_series(directory: Path, name: str, readings) -> Path:
    path = directory / name
    body = [SAMPLE_HEADER]
    body += [f"1,8,{t},{submitted},0,{indexed},0,SERVING"
             for t, submitted, indexed in readings]
    path.write_text("# scylla_version=test\n" + "\n".join(body) + "\n")
    return path


def test_a_levels_start_is_the_first_point_of_its_timeline():
    timeline = growth.index_timeline(rows([(1.0, 100, 100), (2.0, 200, 200)]))

    assert timeline[0] == (0.0, 0.0)


def test_a_count_that_went_backwards_does_not_pull_the_line_back():
    timeline = growth.index_timeline(rows([(1.0, 0, 500), (2.0, 0, 200)]))

    assert [docs for _, docs in timeline] == [0.0, 500.0, 500.0]


def test_a_level_past_the_first_bucket_by_its_first_reading_still_draws():
    """A fast level's first poll can already be several buckets in. Measured
    from the level's start that is simply a fast first bucket; measured from the
    first reading it is a line that breaks before its first point and vanishes
    off the chart with nothing said."""
    timeline = growth.index_timeline(rows([(0.1, 13808, 13808),
                                           (0.2, 27000, 27000),
                                           (0.3, 40000, 40000)]))

    xs, _ = growth.rate_line(timeline, [10000.0, 20000.0, 30000.0])

    assert xs, "the level produced no points at all"


def test_the_first_bucket_is_measured_from_the_start_rather_than_the_first_poll():
    timeline = growth.index_timeline(rows([(1.0, 5000, 5000), (2.0, 10000, 10000)]))

    _, ys = growth.rate_line(timeline, [5000.0, 10000.0])

    assert ys[0] == 5000.0


def test_the_handover_is_the_index_size_when_submitting_stopped():
    readings = rows([(1.0, 5000, 4000), (2.0, 9000, 8000)])
    readings.append({"level": "1", "concurrency": "8", "t_s": "3.0",
                     "docs_submitted": "9000", "submit_docs_per_s": "0",
                     "docs_indexed": "9000", "index_docs_per_s": "0",
                     "index_status": "SERVING"})

    assert growth.handover_docs(readings) == 8000.0


def test_the_closing_row_does_not_put_the_handover_at_zero_documents():
    """That row carries no index count; reading it off the row rather than
    carrying the last one forward marks the handover at the origin."""
    assert growth.handover_docs(rows([(1.0, 5000, 4000), (2.0, 9000, 8000)])) == 8000.0


def test_a_run_that_watched_no_index_is_skipped_rather_than_plotted_as_zero(tmp_path):
    unwatched = tmp_path / "c8-1.csv"
    unwatched.write_text("# scylla_version=test\n" + SAMPLE_HEADER
                         + "\n1,8,1.0,100,100.0,,,\n1,8,2.0,200,100.0,,,\n")

    levels, skipped = growth.load(str(tmp_path / "c*.csv"))

    assert levels == []
    assert "c8-1.csv" in skipped[0]


def test_repetitions_of_one_level_are_one_series(tmp_path):
    readings = [(0.5, 5000, 5000), (1.0, 10000, 10000), (1.5, 15000, 15000)]
    write_series(tmp_path, "c8-1.csv", readings)
    write_series(tmp_path, "c8-2.csv", readings)
    write_series(tmp_path, "c32-1.csv", readings)

    levels, _ = growth.load(str(tmp_path / "c*.csv"))
    grouped = growth.by_series(levels)

    order = growth.series_order(grouped)
    assert order == ["c=8", "c=32"]
    assert [len(grouped[name]) for name in order] == [2, 1]


# --- a searchable count that only advances at a refresh --------------------

def a_level(timeline):
    """A Level built straight from its timeline, without going through a file."""
    level = growth.Level.__new__(growth.Level)
    level.concurrency, level.batch_size, level.repetition = 8, 0, 1
    level.timeline = timeline
    level.handover_docs = None
    return level


SMOOTH = [(0.0, 0.0), (1.0, 100.0), (2.0, 200.0), (3.0, 300.0)]
STAIRCASE = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 900.0),
             (4.0, 900.0), (5.0, 900.0), (6.0, 1800.0)]


def test_a_continuously_climbing_series_sets_no_floor():
    """An index that publishes as it indexes needs no protection, and a floor
    would only coarsen a chart that was already honest."""
    assert growth.riser_floor([a_level(SMOOTH)]) == 0
    assert growth.chosen_step([a_level(SMOOTH)], 10) == 10


def test_a_stepped_series_widens_the_bucket_to_one_riser():
    """A bucket inside a jump is divided by the poll gap rather than the refresh
    gap, so the rate reads high by the ratio between them and the flats vanish.
    """
    assert growth.riser_floor([a_level(STAIRCASE)]) == 900
    assert growth.chosen_step([a_level(STAIRCASE)], 10) == 900


def test_one_slow_poll_does_not_set_the_grid_for_everything():
    """The typical riser, not the largest."""
    hiccup = [(0.0, 0.0), (1.0, 100.0), (2.0, 100.0), (3.0, 200.0),
              (4.0, 200.0), (5.0, 900.0), (6.0, 900.0), (7.0, 1000.0)]
    assert growth.riser_floor([a_level(hiccup)]) == 100


def test_a_stepped_series_protects_a_smooth_one_sharing_the_chart():
    """Both engines on one axis is opt-in, but when it happens the grid has to
    clear the coarser of the two."""
    assert growth.chosen_step([a_level(SMOOTH), a_level(STAIRCASE)], 10) == 900


def test_a_batching_harness_level_is_its_own_series():
    """`c=8` with one document per request and `c=8` with 512 are not the same
    offer and must not share a line."""
    assert growth.name_parts(Path("c8-b512-3.csv")) == (8, 512, 3)
    assert growth.name_parts(Path("c8-2.csv")) == (8, 0, 2)


def test_a_level_where_nothing_became_searchable_is_skipped_by_name():
    """Real zeros, not blanks: an index at `refresh_interval: -1` with no final
    refresh took everything and published none of it. Drawn, it is a flat line
    at zero that reads as an engine finding."""
    rows = [{"t_s": "0.0", "docs_submitted": "10", "docs_indexed": "0"},
            {"t_s": "1.0", "docs_submitted": "10", "docs_indexed": "0"}]
    assert growth.published_nothing(rows)
    assert not growth.published_nothing(
        [{"t_s": "1.0", "docs_submitted": "10", "docs_indexed": "5"}])
