"""Which line a harness row belongs on, now that both halves write one schema.

The chart puts ScyllaDB and every OpenSearch batch size on one axis, and it
decided which was which by whether the row had a `batch_size` column. Both
halves have that column now — `batch_size=1` on a scyllarate row — so the old
test silently relabels every ScyllaDB point as `osrate batch=1`, and where both
globs are given the two lines merge into one and are averaged together. That is
the failure this pins: not a crash, a plausible wrong chart.
"""
import importlib.util
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))


def load_plot_harness_grid():
    """Not a package module — tools/ is a script directory, and the script puts
    BENCH_DIR on sys.path itself the way tools/co_check.py does."""
    path = BENCH_DIR / "tools" / "plot_harness_grid.py"
    spec = importlib.util.spec_from_file_location("plot_harness_grid", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GRID = load_plot_harness_grid()


def a_row(**columns) -> dict:
    return {"concurrency": "8", **columns}


def test_a_scylladb_row_is_not_relabelled_by_its_batch_size_column():
    row = a_row(batch_size="1", engine="scylladb")
    assert GRID.series_of(row, GRID.SCYLLA_ENGINE) == GRID.SCYLLA_SERIES


def test_an_opensearch_row_is_named_by_its_batch_size():
    row = a_row(batch_size="512", engine="opensearch")
    assert GRID.series_of(row, GRID.OPENSEARCH_ENGINE) == "osrate batch=512"


def test_osrate_at_batch_one_is_still_its_own_series():
    """`--batch-size 1` on osrate and one prepared INSERT are different offers,
    and the footer's whole argument is that they must not share a line."""
    row = a_row(batch_size="1", engine="opensearch")
    assert GRID.series_of(row, GRID.OPENSEARCH_ENGINE) == "osrate batch=1"
    assert GRID.series_of(row, GRID.OPENSEARCH_ENGINE) != GRID.SCYLLA_SERIES


def test_the_engine_column_outranks_the_glob_it_arrived_under():
    """A row that ended up under the wrong flag still knows what wrote it."""
    row = a_row(batch_size="1", engine="scylladb")
    assert GRID.series_of(row, GRID.OPENSEARCH_ENGINE) == GRID.SCYLLA_SERIES


def test_a_recorded_csv_from_before_the_engine_column_still_plots():
    """Every CSV under results/ predates both columns, and the ones from the
    first osrate runs have `batch_size` but no `engine`."""
    assert GRID.series_of(a_row(), GRID.SCYLLA_ENGINE) == GRID.SCYLLA_SERIES
    assert (GRID.series_of(a_row(batch_size="512"), GRID.OPENSEARCH_ENGINE)
            == "osrate batch=512")


def test_both_halves_keep_their_own_line_when_both_globs_are_given():
    """The merge this whole column exists to prevent: one ladder each, same
    concurrency, one averaged line."""
    scylla = GRID.series_of(a_row(batch_size="1", engine="scylladb"),
                            GRID.SCYLLA_ENGINE)
    osrate = GRID.series_of(a_row(batch_size="1", engine="opensearch"),
                            GRID.OPENSEARCH_ENGINE)
    assert scylla != osrate
