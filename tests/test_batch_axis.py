"""A chart may not mix two write shapes on one axis and say nothing.

The build-rate axis is outstanding requests, and what one request carries now
differs per engine and per batch level: N documents in one `_bulk` on
OpenSearch, one row in one INSERT on ScyllaDB. That makes two mislabellings
cheap, and both draw as a perfectly ordinary chart:

- one engine's curve built from points taken at different batch levels, which
  then moves for two reasons at once and cannot say which;
- both engines on the documents-per-`_bulk` axis, where b=128 means 128
  documents in a single HTTP request on one side and 128 sequential prepared
  statements on the other.

Silence is the third: a series recorded before the header field existed has no
batch size at all, and reading that as agreement with whatever the other points
recorded is precisely how the S28 concurrency omission became unauditable. So
`None` is a value here, not a wildcard.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))

from ftsbench import sweep_build_rate  # noqa: E402


def load_plot_batch_ceiling():
    """Not a package module — tools/ is a script directory, and the script puts
    BENCH_DIR on sys.path itself the way tools/co_check.py does."""
    path = BENCH_DIR / "tools" / "plot_batch_ceiling.py"
    spec = importlib.util.spec_from_file_location("plot_batch_ceiling", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: its dataclasses are declared under
    # `from __future__ import annotations`, and resolving those looks the owning
    # module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def series(path: Path, engine: str, batch: int | None, docs: int = 100000,
           samples: int = 10) -> Path:
    """A build_monitor series: one header, then samples climbing to `docs`.

    `batch` of None writes no batch_size key at all, which is what a series
    recorded before the field existed looks like.
    """
    header = {"record": "header", "schema_version": 1,
              "producer": "build_monitor", "engine": engine,
              "engine_version": "3.8.0", "label": "unit test",
              "cache_state": "warm-container-fresh-index",
              "corpus": "data/corpus.jsonl", "max_docs": docs,
              "interval_s": 1.0}
    if batch is not None:
        header["batch_size"] = batch
        header["rows_in_flight"] = 1
    lines = [json.dumps(header)]
    for i in range(samples):
        indexed = round(docs * (i + 1) / samples)
        previous = round(docs * i / samples)
        lines.append(json.dumps({
            "record": "sample", "i": i, "t_elapsed_s": float(i),
            "docs_indexed": indexed, "docs_searchable": indexed,
            "docs_delta": indexed - previous,
            "docs_per_s": float(indexed - previous),
            "index_status": "SERVING"}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def point(path: Path):
    loaded = sweep_build_rate.load_point(str(path))
    assert loaded is not None, f"{path} did not load as a point"
    return loaded


def ladder(directory: Path, engine: str, batch: int | None,
           config: str = "opensearch-ramindex", concurrencies=(64, 128)):
    return [point(series(directory / f"c1-{config}-c{conc}-1.jsonl",
                         engine, batch))
            for conc in concurrencies]


def refusal(points) -> str:
    with pytest.raises(SystemExit) as refused:
        sweep_build_rate.assert_one_batch_size_per_engine(points)
    return str(refused.value)


def test_one_batch_size_per_engine_is_what_a_curve_may_be_drawn_from(tmp_path):
    sweep_build_rate.assert_one_batch_size_per_engine(
        ladder(tmp_path, "opensearch", 512))


def test_two_batch_levels_in_one_directory_are_refused(tmp_path):
    """Each level writes to its own OUT_DIR/b<batch>/ directory exactly so this
    cannot happen; a tree that has been flattened by hand must not summarise."""
    points = (ladder(tmp_path, "opensearch", 512)
              + ladder(tmp_path, "opensearch", 64, concurrencies=(192,)))
    message = refusal(points)
    assert "opensearch" in message
    assert "512 documents per operation" in message
    assert "64 documents per operation" in message
    assert "c1-opensearch-ramindex-c192-1.jsonl" in message


def test_the_two_engines_may_each_have_their_own_batch_size(tmp_path):
    """This is the campaign's shape, not a defect: OpenSearch sweeps its wire
    batch while ScyllaDB is pinned at one row per INSERT. The assertion is per
    engine because the axis is requests, which both sides do share."""
    points = (ladder(tmp_path / "os", "opensearch", 512)
              + ladder(tmp_path / "scylla", "scylladb", 1, config="scylla-cdc"))
    sweep_build_rate.assert_one_batch_size_per_engine(points)


def test_an_unrecorded_batch_size_does_not_pass_as_agreement(tmp_path):
    """The S28 retraction turned on a header that recorded no concurrency.
    Treating that silence as "same as the others" is what made the defect
    invisible in the files it produced."""
    points = (ladder(tmp_path, "opensearch", 512)
              + ladder(tmp_path, "opensearch", None, concurrencies=(192,)))
    message = refusal(points)
    assert "UNRECORDED" in message
    assert "c1-opensearch-ramindex-c192-1.jsonl" in message


def test_a_wholly_unrecorded_directory_still_renders(tmp_path):
    """Every series measured before the field existed is equally silent, so
    those trees agree with themselves and must keep summarising — the footer is
    where their silence is disclosed."""
    sweep_build_rate.assert_one_batch_size_per_engine(
        ladder(tmp_path, "opensearch", None))


def test_a_point_that_reaches_no_axis_cannot_break_an_agreement(tmp_path):
    """A series with no value for the plotted metric contributes nothing to the
    picture, so it must contribute nothing to the picture's agreements either —
    otherwise an unplottable stray file blocks a valid chart."""
    drawn = ladder(tmp_path, "opensearch", 512)
    stray = point(series(tmp_path / "c1-opensearch-ramindex-c256-1.jsonl",
                         "opensearch", 64))
    plotted = sweep_build_rate.plotted_points(drawn + [stray], "no_such_metric")
    assert plotted == []
    sweep_build_rate.assert_one_batch_size_per_engine(plotted)


def test_the_axis_caveat_no_longer_claims_equal_x_is_equal_documents():
    """It used to say one operation is --batch-size documents on both sides, so
    equal-x points are comparable. That is now false by design: the batch size
    differs per engine, so equal x is equal request pressure and unequal
    document pressure."""
    caveat = sweep_build_rate.AXIS_CAVEAT
    assert "outstanding requests" in caveat
    assert "never as equal document pressure" in caveat
    assert "BEFORE the loaders were unified" in caveat, \
        "the pre-unification series lost their disclosure"


def test_the_summary_csv_carries_the_shape_beside_the_x_it_was_taken_at():
    """A chart is a picture and a CSV is the record. batch_size sits next to
    concurrency because that is the pair a reader needs to know what a row
    measured."""
    columns = list(sweep_build_rate.CSV_COLUMNS)
    assert columns[columns.index("concurrency") + 1] == "batch_size"
    assert columns[columns.index("batch_size") + 1] == "rows_in_flight"


def batch_tree(root: Path, levels=(64, 512), engine: str = "opensearch",
               config: str = "opensearch-ramindex") -> Path:
    for batch in levels:
        for conc in (64, 128):
            series(root / f"b{batch}" / f"c1-{config}-c{conc}-1.jsonl",
                   engine, batch)
    return root


def test_the_batch_chart_refuses_two_engines_on_one_axis(tmp_path):
    """b=128 is 128 documents in one HTTP request on one side and 128
    sequential prepared statements on the other. Drawing both would render a
    false comparison as a picture, which is harder to withdraw than a number."""
    module = load_plot_batch_ceiling()
    tree = batch_tree(tmp_path)
    series(tree / "b1" / "c1-scylla-cdc-c64-1.jsonl", "scylladb", 1)

    with pytest.raises(SystemExit) as refused:
        module.assert_one_engine(module.load_tree(str(tree)))
    message = str(refused.value)
    assert "one engine" in message
    assert "opensearch" in message and "scylladb" in message
    assert "--batch-size 1" in message, \
        "the message must say why ScyllaDB has no axis, not merely that it is absent"


def test_the_batch_chart_draws_one_engines_levels(tmp_path):
    module = load_plot_batch_ceiling()
    tree = module.load_tree(str(batch_tree(tmp_path)))
    assert module.assert_one_engine(tree) == "opensearch"
    assert sorted(tree) == [64, 512]


def test_a_level_directory_may_not_hold_a_point_from_another_level(tmp_path):
    """The directory name places the point on the x axis and the header says
    what it ran with. When they disagree one of them is wrong, and plotting it
    moves a point along the axis without moving what it measured."""
    module = load_plot_batch_ceiling()
    tree = batch_tree(tmp_path)
    series(tree / "b64" / "c1-opensearch-ramindex-c192-1.jsonl",
           "opensearch", 512)

    with pytest.raises(SystemExit) as refused:
        module.load_tree(str(tree))
    assert f"recorded {sweep_build_rate.batch_label(512)}" in \
        str(refused.value)


def test_a_flat_concurrency_ladder_is_not_a_batch_axis(tmp_path):
    """data/sweep and data/sweep-aws are flat. Reading one as a batch axis
    would put every level's points at one x."""
    module = load_plot_batch_ceiling()
    series(tmp_path / "c1-opensearch-ramindex-c64-1.jsonl", "opensearch", 512)

    with pytest.raises(SystemExit) as refused:
        module.load_tree(str(tmp_path))
    assert "b<batch>/" in str(refused.value)


def test_a_level_directory_may_not_hold_a_point_that_recorded_no_batch(tmp_path):
    """Silence is not agreement, and on this axis it is worse than elsewhere.

    `ftsbench.sweep_build_rate` already treats an unrecorded batch size as its
    own value rather than a wildcard. Here the level lives only in the
    directory name, so a header that records nothing leaves the directory name
    as the sole evidence for the x the point is drawn at — and the sidecar then
    asserts a batch size the series never recorded.
    """
    module = load_plot_batch_ceiling()
    tree = batch_tree(tmp_path)
    series(tree / "b64" / "c1-opensearch-ramindex-c192-1.jsonl",
           "opensearch", None)

    with pytest.raises(SystemExit) as refused:
        module.load_tree(str(tree))
    message = str(refused.value)
    assert sweep_build_rate.batch_label(None) in message, \
        "an absent batch size must read the way the summariser words it"
    assert "c1-opensearch-ramindex-c192-1.jsonl" in message


def test_a_wholly_silent_batch_tree_is_refused_rather_than_plotted(tmp_path):
    """The demonstrated defect: three copies of one batch-500 series whose
    headers carry no batch_size, filed under b16/, b64/ and b512/, drew a solid
    three-point curve with each copy at its directory's x. A flat directory of
    such series still summarises as a concurrency ladder — they agree with each
    other — but a batch axis reads the level off the directory, which is a
    label and not a record."""
    module = load_plot_batch_ceiling()
    for batch in (16, 64, 512):
        series(tmp_path / f"b{batch}" / "c1-opensearch-ramindex-c64-1.jsonl",
               "opensearch", None)

    with pytest.raises(SystemExit) as refused:
        module.load_tree(str(tmp_path))
    assert sweep_build_rate.batch_label(None) in str(refused.value)


def ceilings_file(root: Path, ops_per_s: dict | None = None,
                  **overrides) -> Path:
    """A Phase 0 ceilings document as `ftsbench.client_ceilings` writes it:
    `loader_core_bound_at` appears only where it was measured."""
    document = {"engine": "opensearch",
                "measured_on": "fts-harness i8g.2xlarge",
                "ops_per_s": ops_per_s or {"64": 400.0, "512": 100.0},
                **overrides}
    root.mkdir(parents=True, exist_ok=True)
    path = root / "client-ceilings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def plans_for(module, root: Path, ceilings=None, metric: str | None = None):
    tree = module.load_tree(str(batch_tree(root)))
    args = argparse.Namespace(metric=metric or module.DEFAULT_METRIC)
    return module.plan_configs(tree, {"opensearch-ramindex": 64}, args,
                               ceilings)


def test_a_core_bound_nobody_measured_is_not_read_as_zero(tmp_path):
    """`ftsbench.client_ceilings.ceilings_document` omits the key when the
    constant could not be measured, and `ftsbench.verify_generator` refuses on
    that absence. Defaulting it to 0.0 fabricates a measurement in the artifact
    whose whole job is auditability, and "0 of a core" reads as a stricter bound
    than any real one."""
    module = load_plot_batch_ceiling()
    ceilings = module.load_ceilings(str(ceilings_file(tmp_path)), "opensearch")
    assert ceilings.loader_core_bound_at is None


def test_the_footer_says_the_core_bound_was_not_measured(tmp_path):
    module = load_plot_batch_ceiling()
    ceilings = module.load_ceilings(str(ceilings_file(tmp_path)), "opensearch")
    note = module.ceiling_note(plans_for(module, tmp_path / "tree", ceilings),
                              ceilings)
    assert "under 0" not in note, "the chart claimed a bound of zero cores"
    assert "not measured" in note
    assert "operations/s" in note, \
        "the footer must say what G7 still rests on"


def test_the_footer_still_names_a_core_bound_that_was_measured(tmp_path):
    module = load_plot_batch_ceiling()
    path = ceilings_file(tmp_path, loader_core_bound_at=0.7)
    ceilings = module.load_ceilings(str(path), "opensearch")
    note = module.ceiling_note(plans_for(module, tmp_path / "tree", ceilings),
                              ceilings)
    assert "0.7 of a core" in note


def test_the_sidecar_records_an_unmeasured_core_bound_as_null(tmp_path):
    """The sidecar is the machine-readable half of the same disclosure, so a
    0.0 there survives into whatever reads it next."""
    module = load_plot_batch_ceiling()
    ceilings = module.load_ceilings(str(ceilings_file(tmp_path)), "opensearch")
    recorded = json.loads(json.dumps(module.ceilings_sidecar(ceilings)))
    assert recorded["loader_core_bound_at"] is None


def test_a_metric_that_is_not_a_docs_per_s_rate_is_refused(monkeypatch):
    """G7 divides the plotted metric by the batch size and compares the result
    against an operations/s ceiling; the y label, the per-level summary and the
    best-level annotation all print docs/s. Under --metric stall_fraction the
    division is nonsense and the gate passes every level in silence — which
    turns off the hollow marking whose stated purpose is that a client-bound
    level is never plotted as an engine number."""
    module = load_plot_batch_ceiling()
    monkeypatch.setattr(sys, "argv",
                        ["plot_batch_ceiling.py", "--c-sat",
                         "opensearch-ramindex:64", "--metric",
                         "stall_fraction"])
    with pytest.raises(SystemExit) as refused:
        module.parse_args()
    message = str(refused.value)
    assert "stall_fraction" in message
    assert module.DEFAULT_METRIC in message, \
        "a refusal has to name a metric the chart will accept"


def test_every_docs_per_s_column_of_the_summary_stays_plottable(tmp_path):
    """The accepted set is read against what `build_report.summarize` actually
    produces, so a new docs/s column does not quietly become unplottable."""
    module = load_plot_batch_ceiling()
    assert module.DEFAULT_METRIC in module.RATE_METRICS
    summary = point(series(tmp_path / "c1-opensearch-ramindex-c64-1.jsonl",
                           "opensearch", 512)).summary
    rates = {name for name in summary if name.startswith("docs_per_s")}
    assert rates and rates <= module.RATE_METRICS


def test_the_series_pattern_did_not_have_to_learn_the_level(tmp_path):
    """The level lives in the directory precisely so no filename token had to
    be added. If SERIES_RE ever grew one, every series in results/ would stop
    parsing and every published chart would become unrenderable."""
    tree = batch_tree(tmp_path, levels=(512,))
    path = tree / "b512" / "c1-opensearch-ramindex-c64-1.jsonl"
    match = sweep_build_rate.SERIES_RE.search(path.name)
    assert match.group("config") == "opensearch-ramindex"
    assert (match.group("concurrency"), match.group("rep")) == ("64", "1")
