"""The globs must match the files the harness actually wrote, and nothing else.

Both halves of that sentence have already failed in this campaign. A glob that
matches too much folded the refresh=30s repetitions into plain OpenSearch; a
glob that matches too little (C7's `.jsonl`, where the sweep writes `.json`)
draws a configuration as a labelled absence, which a viewer reads as a finding
about the engine rather than about the harness.

The expected filenames are spelled out here rather than built with the module's
own helpers. The Makefile is what names the artifacts, so a test that asked
`render_results` what it expects would agree with itself and prove nothing.
"""
import fnmatch
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))

from ftsbench import render_results, results_tree  # noqa: E402

REPS = (1, 2, 3, 4, 5)


def args_for(**overrides):
    defaults = {"run_name": "test-run", "results_root": "results",
                "data_dir": "data", "footer_extra": "footer",
                "copy_raw": True, "dry_run": False}
    return type("Args", (), {**defaults, **overrides})()


def artifact_name(chart, config, rep):
    """The Makefile's naming, written out: `c5-<config>-<class>-<rep>.jsonl`,
    `c7-<config>-<rep>.json`, `c1-<config>-<rep>.jsonl`."""
    infix = f"-{chart.class_infix}" if chart.class_infix else ""
    return f"{chart.series_prefix}-{config}{infix}-{rep}.{chart.extension}"


def every_artifact_name(chart):
    return {config: [artifact_name(chart, config, rep) for rep in REPS]
            for config in render_results.EVERY_CONFIG}


def matches(pattern, name):
    return fnmatch.fnmatch(name, pattern.removeprefix("data/"))


@pytest.mark.parametrize("chart", render_results.CHARTS, ids=lambda c: c.chart_id)
def test_each_config_glob_matches_only_that_configs_files(chart):
    names = every_artifact_name(chart)
    for config in chart.configs:
        pattern = render_results.artifact_glob(chart, config, "data")
        for other, other_names in names.items():
            for name in other_names:
                assert matches(pattern, name) == (other == config), \
                    f"{chart.chart_id} {config} glob {pattern} vs {name}"


@pytest.mark.parametrize("chart", render_results.CHARTS, ids=lambda c: c.chart_id)
def test_the_plain_opensearch_glob_excludes_the_refresh30_repetitions(chart):
    """The two OpenSearch configurations differ only in refresh_interval, so a
    glob that swept both would compare a configuration against a mixture
    containing itself — and the chart would draw without complaint."""
    pattern = render_results.artifact_glob(chart, render_results.OPENSEARCH, "data")
    for rep in REPS:
        contaminant = artifact_name(chart, render_results.OPENSEARCH_REFRESH30, rep)
        assert not matches(pattern, contaminant), f"{pattern} matched {contaminant}"


@pytest.mark.parametrize("chart", render_results.CHARTS, ids=lambda c: c.chart_id)
def test_each_config_glob_matches_every_repetition(chart):
    """A glob matching only some repetitions silently lowers N, and N is printed
    in the chart footer as if it were the number measured."""
    for config in chart.configs:
        pattern = render_results.artifact_glob(chart, config, "data")
        for rep in REPS:
            name = artifact_name(chart, config, rep)
            assert matches(pattern, name), f"{pattern} missed {name}"


def test_c7_globs_the_extension_the_sweep_writes():
    """C7_OS_OUT in the Makefile ends in .json. plot_c7's docstring says .jsonl,
    which matches nothing."""
    pattern = render_results.artifact_glob(chart_by_id("C7"),
                                           render_results.OPENSEARCH, "data")
    assert matches(pattern, "c7-opensearch-1.json")
    assert not matches(pattern, "c7-opensearch-1.jsonl")


def chart_by_id(chart_id):
    for chart in render_results.CHARTS:
        if chart.chart_id == chart_id:
            return chart
    pytest.fail(f"no chart {chart_id}")


def test_c2_is_drawn_from_the_c1_series():
    """C2 has no artifact of its own: time-to-searchable is read out of the C1
    build series. A `c2-*` glob would match nothing on disk."""
    assert chart_by_id("C2").series_prefix == "c1"


def test_c3_names_its_own_manifests():
    """C3 is a second, separately-manifested ingest run — `manifest-c3-*` — not
    the C1 build. Globbing `manifest-<config>-*` would attribute C1's tuning to
    it."""
    pattern = render_results.manifest_glob(chart_by_id("C3"),
                                           render_results.SCYLLA_CDC, "data")
    assert matches(pattern, "manifest-c3-scylla-cdc-1.json")
    assert not matches(pattern, "manifest-scylla-cdc-1.json")


def test_c3_skips_the_bootstrap_path():
    """The bootstrap path indexes an already-loaded table, so it has no ingest
    window to measure a write tail over, and writes no c3 artifact."""
    assert render_results.SCYLLA_BOOTSTRAP not in chart_by_id("C3").configs


def test_all_eight_charts_are_rendered_once():
    ids = [chart.chart_id for chart in render_results.CHARTS]
    assert ids == [f"C{n}" for n in range(1, 9)]


@pytest.mark.parametrize("chart", render_results.CHARTS, ids=lambda c: c.chart_id)
def test_the_plot_command_is_accepted_by_its_module(chart):
    """A renamed flag fails only when the chart is finally drawn, which is after
    the campaign has been run."""
    import importlib
    module = importlib.import_module(chart.module)
    command = render_results.plot_command(chart, args_for())
    argv = command[command.index("-m") + 2:]
    saved = sys.argv
    sys.argv = [chart.module] + argv
    try:
        module.parse_args()
    except SystemExit as exit_signal:
        if exit_signal.code not in (0, None):
            pytest.fail(f"{chart.module} rejected: {' '.join(argv)}")
    finally:
        sys.argv = saved


@pytest.mark.parametrize("chart", render_results.CHARTS, ids=lambda c: c.chart_id)
def test_the_chart_spec_parses_back_to_what_was_drawn(chart):
    args = args_for()
    spec = results_tree.parse_chart_spec(render_results.chart_spec(chart, args))
    assert spec.chart_id == chart.chart_id
    assert spec.png == render_results.png_path(chart, args.results_root)
    assert spec.sidecar == render_results.sidecar_path(chart, args.results_root)
    assert len(spec.artifacts) == len(chart.configs)
    assert len(spec.manifests) == len(chart.configs)


def test_the_tree_is_assembled_after_every_chart():
    """results_tree reads the sidecars, so a tree built before the last chart
    describes the previous run."""
    commands = render_results.commands(args_for())
    modules = [command[command.index("-m") + 1] for command in commands]
    assert modules[-1] == render_results.RESULTS_TREE
    assert modules[:-1] == [chart.module for chart in render_results.CHARTS]


def test_dry_run_executes_nothing(monkeypatch, capsys):
    def refuse(*_args, **_kwargs):
        pytest.fail("--dry-run ran a command")

    monkeypatch.setattr(render_results.subprocess, "run", refuse)
    monkeypatch.setattr(sys, "argv",
                        ["render_results", "--run-name", "x", "--dry-run"])
    assert render_results.main() == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == len(render_results.CHARTS) + 1


def test_one_failed_chart_does_not_stop_the_rest(monkeypatch, capsys):
    """Seven charts and a named failure is more useful than an aborted tree."""
    attempted = []

    def fake_run(command, *_args, **_kwargs):
        attempted.append(command)
        failing = "ftsbench.plot_c4" in command
        return type("Completed", (), {"returncode": 1 if failing else 0})()

    monkeypatch.setattr(render_results.subprocess, "run", fake_run)
    assert render_results.render(args_for()) == 1
    assert len(attempted) == len(render_results.CHARTS) + 1
    assert "plot_c4" in capsys.readouterr().err


def test_the_make_target_actually_runs_the_renderer():
    """`results/` exists on disk, so a target named `results` that is not in
    .PHONY is considered already made and make prints nothing at all."""
    import subprocess
    result = subprocess.run(["make", "-n", "results"], cwd=BENCH_DIR,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ftsbench.render_results" in result.stdout, \
        "make -n results rendered nothing — the target is shadowed by results/"
