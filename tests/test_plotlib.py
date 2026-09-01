"""Tests for the shared chart plumbing and the C2-C8 consumers.

Every fixture here is synthetic and named so: the values are invented to make a
consumer's behaviour observable, and no number in this file is a measurement of
anything. The artifacts are built from SCHEMAS.md field names only, which is
also what the chart modules are written against.

The properties under test are the ones that would silently produce a wrong
chart rather than an error: plotting the median repetition instead of the mean,
refusing a percentile the sample cannot carry, keeping a generator-bound point
out of the SLA knee, and never drawing a zero bar for an index that lives in
RAM.
"""
import functools
import json
import math
import os
import sys
from typing import Any, Callable, Sequence

import pytest

from ftsbench import (plot_c1, plot_c2, plot_c3, plot_c4, plot_c5, plot_c6,
                      plot_c7, plot_c8, plotlib, results_tree)

SYNTHETIC_LABEL = "SYNTHETIC pytest fixture — invented values, not a measurement"
SYNTHETIC_CORPUS = "data/synthetic-corpus-not-a-measurement.jsonl"


def synthetic_header(engine: str, **extra: Any) -> dict[str, Any]:
    return {"record": "header", "schema_version": 1, "engine": engine,
            "engine_version": "0.0.0-synthetic", "label": SYNTHETIC_LABEL,
            "cache_state": "cold", "corpus": SYNTHETIC_CORPUS,
            "git_commit": "0000000", "host": {"hostname": "synthetic-host"},
            **extra}


def write_series(directory: Any, name: str, engine: str,
                 records: Sequence[dict[str, Any]], **header: Any) -> str:
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as out:
        for record in (synthetic_header(engine, **header), *records):
            out.write(json.dumps(record) + "\n")
    return path


def series_set(directory: Any, chart: str, config: str, engine: str,
               per_repetition: Sequence[Sequence[dict[str, Any]]],
               **header: Any) -> str:
    """Writes one artifact per repetition and returns its `NAME:GLOB` spec."""
    for index, records in enumerate(per_repetition, start=1):
        write_series(directory, f"synthetic-{chart}-{config}-{index}.jsonl",
                     engine, records, **header)
    pattern = os.path.join(str(directory), f"synthetic-{chart}-{config}-*.jsonl")
    return f"{config}:{pattern}"


def parsed(module: Any, monkeypatch: pytest.MonkeyPatch,
           argv: Sequence[str]) -> Any:
    monkeypatch.setattr(sys, "argv", ["synthetic-test", *argv])
    return module.parse_args()


def rendered(module: Any, monkeypatch: pytest.MonkeyPatch, argv: Sequence[str],
             record: str,
             metric: Callable[[plotlib.Run], float]) -> tuple[Any, dict[str, Any]]:
    args = parsed(module, monkeypatch, argv)
    configs = plotlib.collect_configs(args.config, record, metric)
    return module.plot(args, configs)


def latency_op(index: int, latency_ms: float, **extra: Any) -> dict[str, Any]:
    return {"record": "latency_op", "i": index, "t_intended_s": index * 0.01,
            "t_start_s": index * 0.01, "t_end_s": index * 0.01 + latency_ms / 1000,
            "latency_ms": latency_ms, "service_ms": latency_ms, "queue_ms": 0.0,
            "ok": True, **extra}


def searches(count: int, latency_ms: float, **extra: Any) -> list[dict[str, Any]]:
    return [latency_op(index, latency_ms + index % 7, op="search", hits=3, **extra)
            for index in range(count)]


def build_sample(elapsed_s: float, indexed: int, searchable: int,
                 status: str) -> dict[str, Any]:
    return {"record": "sample", "t_elapsed_s": elapsed_s, "docs_indexed": indexed,
            "docs_searchable": searchable, "docs_per_s": 0.0,
            "docs_per_s_cumulative": 0.0, "index_status": status}


def resource_sample(elapsed_s: float, role: str, rss_bytes: int,
                    index_size_bytes: int | None, **extra: Any) -> dict[str, Any]:
    return {"record": "resource_sample", "t_elapsed_s": elapsed_s,
            "container": f"synthetic-{role}", "role": role,
            "rss_bytes": rss_bytes, "cache_bytes": 0, "cpu_cores_used": 1.0,
            "index_size_bytes": index_size_bytes, "running": True, **extra}


def sweep_point(offered: float, achieved: float, p99_ms: float,
                queue_p99_ms: float, saturated: bool,
                ceiling: float = 2000.0, count: int | None = None,
                errors: int = 0) -> dict[str, Any]:
    return {"record": "sweep_point", "offered_qps": offered,
            "achieved_qps": achieved, "duration_s": 60.0, "warmup_s": 10.0,
            "count": int(achieved * 60) if count is None else count,
            "errors": errors, "p50_ms": p99_ms / 4,
            "p95_ms": p99_ms / 2, "p99_ms": p99_ms, "p999_ms": p99_ms * 2,
            "max_ms": p99_ms * 3, "queue_p99_ms": queue_p99_ms,
            "generator_ceiling_qps": ceiling, "generator_saturated": saturated}


def freshness_probe(index: int, lag_s: float | None,
                    timed_out: bool = False) -> dict[str, Any]:
    return {"record": "freshness_probe", "i": index,
            "marker": f"ftsfreshsynthetic{index:04d}", "t_write_s": 1.0 + index,
            "t_searchable_s": None if lag_s is None else 1.0 + index + lag_s,
            "lag_s": lag_s, "poll_interval_s": 0.05, "engine": "synthetic",
            "refresh_interval": "1s", "timed_out": timed_out}


def all_text(figure: Any) -> str:
    texts = [item.get_text() for item in figure.texts]
    for axes in figure.axes:
        texts.extend(item.get_text() for item in axes.texts)
        texts.append(axes.get_title())
    return "\n".join(texts)


def bar_heights(axes: Any) -> list[float]:
    return [patch.get_height() for patch in axes.patches]


def test_median_index_picks_the_median_and_not_the_mean():
    """The mean of these is 34.3, whose nearest repetition is the 100 outlier."""
    assert plotlib.median_index([1.0, 2.0, 100.0]) == 1


def test_median_index_of_an_even_count_is_a_real_repetition():
    """Index 1 is the lower middle, so the drawn series is always measured."""
    assert plotlib.median_index([10.0, 20.0, 30.0, 40.0]) == 1


def test_collect_configs_plots_the_median_repetition(tmp_path):
    spec = series_set(tmp_path, "c5", "opensearch", "opensearch",
                      [searches(200, 900.0), searches(200, 100.0),
                       searches(200, 10.0)])
    config = plotlib.collect_configs([spec], "latency_op",
                                     plot_c5.run_search_p99)[0]
    assert os.path.basename(config.chosen.path).endswith("-2.jsonl")
    assert config.chosen_metric == pytest.approx(sorted(config.metrics)[1])


def test_the_plotted_metric_is_not_the_mean_of_the_repetitions(tmp_path):
    spec = series_set(tmp_path, "c5", "opensearch", "opensearch",
                      [searches(200, 10.0), searches(200, 12.0),
                       searches(200, 900.0)])
    config = plotlib.collect_configs([spec], "latency_op",
                                     plot_c5.run_search_p99)[0]
    mean = sum(config.metrics) / len(config.metrics)
    assert config.chosen_metric < mean / 10


def test_the_sidecar_carries_the_spread_of_every_repetition(tmp_path,
                                                            monkeypatch):
    spec = series_set(tmp_path, "c5", "opensearch", "opensearch",
                      [searches(200, 10.0), searches(200, 20.0),
                       searches(200, 30.0)])
    output = os.path.join(str(tmp_path), "synthetic-c5.png")
    args = parsed(plot_c5, monkeypatch, ["--config", spec, "--output", output])
    configs = plotlib.collect_configs(args.config, "latency_op",
                                      plot_c5.run_search_p99)
    document = plotlib.sidecar_document(args, configs, {"metric_name": "synthetic"})
    recorded = document["configs"]["opensearch"]
    assert len(recorded["repetition_metric_values"]) == 3
    assert set(recorded["repetition_metric_spread"]) == {"min", "max", "median"}


def test_load_runs_skips_an_artifact_that_holds_no_matching_record(tmp_path):
    write_series(tmp_path, "synthetic-mix-1.jsonl", "opensearch",
                 [build_sample(1.0, 10, 10, "SERVING")])
    write_series(tmp_path, "synthetic-mix-2.jsonl", "opensearch",
                 searches(150, 4.0))
    pattern = os.path.join(str(tmp_path), "synthetic-mix-*.jsonl")
    runs = plotlib.load_runs("mix", pattern, "latency_op")
    assert [os.path.basename(run.path) for run in runs] == ["synthetic-mix-2.jsonl"]


def test_percentiles_below_the_sample_floor_are_not_offered():
    assert plotlib.supported_percentiles(1_500, [99.0, 99.9, 99.99]) == [99.0, 99.9]


def test_a_refusal_note_states_the_sample_count_it_would_need():
    note = plotlib.refusal_note("thin", 1_500, [99.99])
    assert "10,000" in note and "1,500" in note and "not drawn" in note


def test_failed_operations_are_excluded_from_latencies_but_counted(tmp_path):
    records = [latency_op(0, 5.0, op="search"),
               {**latency_op(1, 0.0, op="search"), "ok": False,
                "error": "synthetic failure"}]
    assert plotlib.latency_ms_values(records) == [5.0]
    assert plotlib.error_count(records) == 1


def test_the_footer_names_versions_cache_state_repetitions_and_corpus(tmp_path,
                                                                     monkeypatch):
    spec = series_set(tmp_path, "c5", "scylla-cdc", "scylladb",
                      [searches(150, 3.0), searches(150, 4.0)])
    args = parsed(plot_c5, monkeypatch, ["--config", spec])
    configs = plotlib.collect_configs(args.config, "latency_op",
                                      plot_c5.run_search_p99)
    footer = plotlib.footer_text(configs, args)
    assert "0.0.0-synthetic" in footer and "cache=cold" in footer
    assert "N=2" in footer and os.path.basename(SYNTHETIC_CORPUS) in footer


def test_the_write_path_disclosure_rides_in_the_footer_not_a_footnote(tmp_path,
                                                                     monkeypatch):
    spec = series_set(tmp_path, "c3", "scylla-cdc", "scylladb",
                      [[latency_op(i, 2.0, op="insert", n_docs=1)
                        for i in range(200)]])
    args = parsed(plot_c3, monkeypatch, ["--config", spec])
    configs = plotlib.collect_configs(args.config, "latency_op",
                                      plot_c3.run_write_p99)
    assert "CDC hop" in plotlib.footer_text(configs, args)


def test_the_preliminary_stamp_is_on_by_default(monkeypatch):
    args = parsed(plot_c5, monkeypatch, ["--config", "x:none-*.jsonl"])
    assert args.stamp is True
    assert plotlib.PRELIMINARY_STAMP in plotlib.sidecar_document(
        args, [], {})["preliminary_stamp"]


def test_c2_derives_the_scylla_time_from_the_first_serving_sample(tmp_path):
    samples = [build_sample(1.0, 100, 0, "BUILDING"),
               build_sample(2.0, 300, 300, "SERVING"),
               build_sample(3.0, 300, 300, "SERVING")]
    spec = series_set(tmp_path, "c2", "scylla-cdc", "scylladb", [samples],
                      max_docs=300)
    config = plotlib.collect_configs([spec], "sample",
                                     plot_c2.time_to_searchable)[0]
    outcome = plot_c2.outcome_for(config, 0)
    assert outcome.seconds == pytest.approx(2.0)
    assert "SERVING" in outcome.condition


def test_c2_derives_the_opensearch_time_from_docs_searchable_reaching_the_corpus(
        tmp_path):
    samples = [build_sample(1.0, 300, 100, "green"),
               build_sample(2.0, 300, 299, "green"),
               build_sample(3.0, 300, 300, "green")]
    spec = series_set(tmp_path, "c2", "opensearch", "opensearch", [samples],
                      max_docs=300)
    config = plotlib.collect_configs([spec], "sample",
                                     plot_c2.time_to_searchable)[0]
    outcome = plot_c2.outcome_for(config, 0)
    assert outcome.seconds == pytest.approx(3.0)
    assert "docs_searchable" in outcome.condition


def test_c2_leaves_a_run_unresolved_when_its_condition_never_fires(tmp_path):
    samples = [build_sample(1.0, 300, 100, "green"),
               build_sample(2.0, 300, 200, "green")]
    spec = series_set(tmp_path, "c2", "opensearch", "opensearch", [samples],
                      max_docs=300)
    config = plotlib.collect_configs([spec], "sample",
                                     plot_c2.time_to_searchable)[0]
    outcome = plot_c2.outcome_for(config, 0)
    assert config.metrics == [plotlib.UNRESOLVED]
    assert outcome.seconds is None
    assert outcome.unresolved_files


def test_c2_marks_a_serving_status_that_fired_before_the_corpus_was_indexed(
        tmp_path):
    samples = [build_sample(1.0, 120, 120, "SERVING"),
               build_sample(4.0, 300, 300, "SERVING")]
    spec = series_set(tmp_path, "c2", "scylla-cdc", "scylladb", [samples],
                      max_docs=300)
    config = plotlib.collect_configs([spec], "sample",
                                     plot_c2.time_to_searchable)[0]
    outcome = plot_c2.outcome_for(config, 0)
    assert outcome.lower_bound is True
    assert outcome.all_docs_seconds == pytest.approx(4.0)


def test_c3_leaves_a_gap_where_a_bucket_cannot_carry_the_percentile():
    thin = {0: [1.0] * 150, 1: [2.0] * 10}
    times, values, omitted = plot_c3.bucket_series(thin, 5.0, 99.0)
    assert omitted == 1
    assert not math.isnan(values[0]) and math.isnan(values[1])
    assert times == [2.5, 7.5]


def test_c4_never_draws_a_zero_bar_for_an_index_that_lives_in_ram(tmp_path,
                                                                 monkeypatch):
    records = [resource_sample(1.0, "scylladb", 4_000_000_000, 2_000_000_000),
               resource_sample(1.0, "vector-store", 6_000_000_000, None)]
    spec = series_set(tmp_path, "c4", "scylla-cdc", "scylladb", [records])
    output = os.path.join(str(tmp_path), "synthetic-c4.png")
    figure, extras = rendered(plot_c4, monkeypatch,
                              ["--config", spec, "--output", output],
                              "resource_sample", plot_c4.run_peak_total_rss)
    index_axes = figure.axes[2]
    assert 0.0 not in bar_heights(index_axes)
    assert plot_c4.IN_RAM_NOTE in "\n".join(text.get_text()
                                            for text in index_axes.texts)
    roles = extras["per_config"]["scylla-cdc"]["roles"]
    in_ram = [role for role in roles if role["role"] == "vector-store"][0]
    assert in_ram["index_size_bytes"] is None
    assert in_ram["index_in_ram"] is True


def test_c4_sums_the_scylla_roles_as_well_as_splitting_them(tmp_path,
                                                            monkeypatch):
    records = [resource_sample(1.0, "scylladb", 4_000_000_000, 2_000_000_000),
               resource_sample(1.0, "vector-store", 6_000_000_000, None)]
    spec = series_set(tmp_path, "c4", "scylla-cdc", "scylladb", [records])
    output = os.path.join(str(tmp_path), "synthetic-c4.png")
    _, extras = rendered(plot_c4, monkeypatch,
                         ["--config", spec, "--output", output],
                         "resource_sample", plot_c4.run_peak_total_rss)
    roles = {role["role"]: role for role in
             extras["per_config"]["scylla-cdc"]["roles"]}
    assert set(roles) == {"scylladb", "vector-store", "total"}
    assert roles["total"]["peak_rss_bytes"] == pytest.approx(10_000_000_000)


def c1_sample(elapsed_s: float, docs: int, rate: float, delta: int = 0,
              **extra: Any) -> dict[str, Any]:
    return {"record": "sample", "i": int(elapsed_s), "t_elapsed_s": elapsed_s,
            "docs_indexed": docs, "docs_delta": delta, "docs_per_s": rate,
            "docs_per_s_cumulative": rate, "index_status": "SERVING", **extra}


C1_BUILD = [c1_sample(0.0, 0, 0.0), c1_sample(1.0, 100, 100.0, delta=100),
            c1_sample(2.0, 200, 100.0, delta=100),
            c1_sample(3.0, 300, 100.0, delta=100)]


def test_c1_sidecar_carries_the_fields_a_chart_readme_reads(tmp_path, monkeypatch):
    """results_tree renders UNRECORDED for anything the sidecar omits, and a
    chart whose write-up cannot state its own confidence tier is the one most
    likely to be quoted as if it had none."""
    spec = series_set(tmp_path, "c1", "opensearch", "opensearch", [C1_BUILD])
    output = os.path.join(str(tmp_path), "synthetic-c1.png")
    monkeypatch.setattr(sys, "argv",
                        ["synthetic-test", "--config", spec, "--output", output])
    assert plot_c1.main() == 0
    document = json.load(open(f"{os.path.splitext(output)[0]}.json",
                              encoding="utf-8"))
    for field in ("claim", "claim_status", "confidence_tier", "aws_delta",
                  "x_axis", "y_axis", "run_selection"):
        assert document[field], f"C1 sidecar omits {field}"
    assert document["configs"]["opensearch"]["metric"] == plot_c1.METRIC_NAME


def test_c1_drops_a_repetition_with_no_build_window(tmp_path, monkeypatch):
    """A series can hold samples and still hold no measurable build. Scoring it
    zero would make it a candidate for the median and could put a failed
    repetition on the chart."""
    idle = [c1_sample(0.0, 0, 0.0), c1_sample(1.0, 0, 0.0)]
    spec = series_set(tmp_path, "c1", "opensearch", "opensearch", [C1_BUILD, idle])
    output = os.path.join(str(tmp_path), "synthetic-c1-partial.png")
    monkeypatch.setattr(sys, "argv",
                        ["synthetic-test", "--config", spec, "--output", output])
    assert plot_c1.main() == 0
    document = json.load(open(f"{os.path.splitext(output)[0]}.json",
                              encoding="utf-8"))
    assert document["configs"]["opensearch"]["repetitions"] == 1


def test_c4_ignores_the_first_tick_that_reported_no_cpu(tmp_path, monkeypatch):
    """cpu_cores_used is a delta between two counter reads, so the first sample
    reports null. Averaged as a zero it would drag every CPU bar below the
    truth, and on a short run visibly so."""
    records = [resource_sample(1.0, "opensearch", 4_000_000_000, 1,
                               i=0, cpu_cores_used=None),
               resource_sample(2.0, "opensearch", 4_000_000_000, 1,
                               i=1, cpu_cores_used=3.0),
               resource_sample(3.0, "opensearch", 4_000_000_000, 1,
                               i=2, cpu_cores_used=3.0)]
    spec = series_set(tmp_path, "c4", "opensearch", "opensearch", [records])
    output = os.path.join(str(tmp_path), "synthetic-c4-cpu.png")
    _, extras = rendered(plot_c4, monkeypatch,
                         ["--config", spec, "--output", output],
                         "resource_sample", plot_c4.run_peak_total_rss)
    role = extras["per_config"]["opensearch"]["roles"][0]
    assert role["mean_cpu_cores_used"] == pytest.approx(3.0)


def test_c4_annotates_a_config_whose_container_was_not_running(tmp_path,
                                                              monkeypatch):
    """A container that exits mid-run keeps producing samples. Averaging them
    reports a footprint for a process that was gone, which is the difference
    between a cheap engine and a dead one."""
    records = [resource_sample(1.0, "opensearch", 4_000_000_000, 1, i=0),
               resource_sample(2.0, "opensearch", 0, 1, i=1, running=False)]
    spec = series_set(tmp_path, "c4", "opensearch", "opensearch", [records])
    output = os.path.join(str(tmp_path), "synthetic-c4-dead.png")
    _, extras = rendered(plot_c4, monkeypatch,
                         ["--config", spec, "--output", output],
                         "resource_sample", plot_c4.run_peak_total_rss)
    role = extras["per_config"]["opensearch"]["roles"][0]
    assert role["ticks_not_running"] == 1
    notes = "\n".join(extras["notes"])
    assert "NOT RUNNING" in notes and "opensearch/opensearch (1 samples)" in notes


def test_c5_refuses_a_percentile_the_sample_cannot_support(tmp_path, monkeypatch):
    spec = series_set(tmp_path, "c5", "thin", "opensearch",
                      [searches(1_500, 5.0), searches(1_500, 6.0)])
    output = os.path.join(str(tmp_path), "synthetic-c5.png")
    figure, extras = rendered(plot_c5, monkeypatch,
                              ["--config", spec, "--output", output],
                              "latency_op",
                              functools.partial(plot_c5.run_search_p99,
                                                query_class=""))
    summary = extras["per_config"]["thin"]
    assert summary["percentiles_refused"] == ["p99.99"]
    assert "p99.99" not in summary["percentiles_drawn"]
    assert "p99.99" in all_text(figure)
    assert any("p99.99" in note for note in extras["notes"])


def test_c5_draws_a_percentile_the_sample_does_support(tmp_path, monkeypatch):
    spec = series_set(tmp_path, "c5", "deep", "opensearch", [searches(10_000, 5.0)])
    output = os.path.join(str(tmp_path), "synthetic-c5.png")
    _, extras = rendered(plot_c5, monkeypatch,
                         ["--config", spec, "--output", output], "latency_op",
                         functools.partial(plot_c5.run_search_p99, query_class=""))
    summary = extras["per_config"]["deep"]
    assert summary["percentiles_refused"] == []
    assert "p99.99" in summary["percentiles_drawn"]
    assert summary["percentiles_unstable"] == ["p99.99"]


def test_c6_reports_all_six_query_classes_even_when_one_has_no_queries(tmp_path,
                                                                      monkeypatch):
    present = [name for name in plotlib.QUERY_CLASSES if name != "bool_mixed"]
    records = [record for name in present
               for record in searches(150, 4.0, **{"class": name})]
    spec = series_set(tmp_path, "c6", "opensearch", "opensearch", [records])
    output = os.path.join(str(tmp_path), "synthetic-c6.png")
    figure, extras = rendered(plot_c6, monkeypatch,
                              ["--config", spec, "--output", output],
                              "latency_op", plot_c6.run_p50)
    per_class = extras["per_config"]["opensearch"]["per_class"]
    assert list(per_class) == list(plotlib.QUERY_CLASSES)
    assert per_class["bool_mixed"]["queries"] == 0
    assert "no data" in all_text(figure)


def test_c7_keeps_a_saturated_point_out_of_the_knee():
    points = [sweep_point(200, 199.5, 20.0, 1.0, False),
              sweep_point(1_800, 1_700.0, 30.0, 12.0, True)]
    knee = plot_c7.knee_point(points, 50.0)
    assert knee is not None
    assert knee["offered_qps"] == 200


def test_c7_reports_no_knee_when_only_saturated_points_met_the_sla():
    points = [sweep_point(1_800, 1_700.0, 30.0, 12.0, True),
              sweep_point(200, 199.5, 90.0, 1.0, False)]
    assert plot_c7.knee_point(points, 50.0) is None
    summary = plot_c7.config_summary(points, None, 50.0)
    assert summary["knee_status"] == plot_c7.NO_KNEE


def test_c7_marks_saturated_points_differently_and_says_how_many(tmp_path,
                                                                monkeypatch):
    points = [sweep_point(200, 199.5, 20.0, 1.0, False),
              sweep_point(1_800, 1_700.0, 30.0, 12.0, True)]
    spec = series_set(tmp_path, "c7", "opensearch", "opensearch", [points])
    output = os.path.join(str(tmp_path), "synthetic-c7.png")
    figure, extras = rendered(plot_c7, monkeypatch,
                              ["--config", spec, "--output", output],
                              "sweep_point", plot_c7.max_unsaturated_qps)
    summary = extras["per_config"]["opensearch"]
    assert summary["points_saturated"] == 1
    markers = {line.get_marker() for line in figure.axes[0].lines
               if line.get_marker() not in ("", "None")}
    assert len(markers) > 1
    assert any("generator" in note for note in extras["notes"])


def test_c7_draws_the_generator_ceiling_as_its_own_annotated_line(tmp_path,
                                                                 monkeypatch):
    points = [sweep_point(200, 199.5, 20.0, 1.0, False, ceiling=2_000.0),
              sweep_point(1_800, 1_700.0, 30.0, 12.0, True, ceiling=2_000.0)]
    spec = series_set(tmp_path, "c7", "opensearch", "opensearch", [points])
    output = os.path.join(str(tmp_path), "synthetic-c7.png")
    figure, extras = rendered(plot_c7, monkeypatch,
                              ["--config", spec, "--output", output],
                              "sweep_point", plot_c7.max_unsaturated_qps)
    assert extras["per_config"]["opensearch"]["generator_ceiling_qps"] == 2_000.0
    verticals = [line.get_xdata()[0] for line in figure.axes[0].lines
                 if len(set(line.get_xdata())) == 1]
    assert 2_000.0 in verticals
    assert "2,000" in all_text(figure)


def test_c7_flags_a_recorded_flag_that_disagrees_with_the_schema_rule():
    lying = sweep_point(1_900, 1_000.0, 30.0, 20.0, False)
    assert plot_c7.rule_saturated(lying)
    assert plot_c7.flag_disagreements([lying]) == [1_900.0]


def test_c4_drops_a_repetition_whose_container_never_ran(tmp_path, monkeypatch):
    """Median selection over two repetitions takes the lower score, and a run
    with no container scores near zero — so the crashed repetition would win the
    median and the chart would report a footprint for a process that was absent."""
    live = [resource_sample(1.0, "opensearch", 4_000_000_000, 900_000_000),
            resource_sample(2.0, "opensearch", 4_200_000_000, 950_000_000)]
    dead = [resource_sample(1.0, "opensearch", 0, None, running=False),
            resource_sample(2.0, "opensearch", 0, None, running=False)]
    spec = series_set(tmp_path, "c4", "opensearch", "opensearch", [dead, live])
    output = os.path.join(str(tmp_path), "synthetic-c4-dead.png")
    monkeypatch.setattr(sys, "argv",
                        ["synthetic-test", "--config", spec, "--output", output])
    assert plot_c4.main() == 0
    document = json.load(open(f"{os.path.splitext(output)[0]}.json",
                              encoding="utf-8"))
    config = document["configs"]["opensearch"]
    assert config["repetitions"] == 1
    assert config["chosen_file"].endswith("-2.jsonl"), "the live run, not the dead one"
    assert min(config["repetition_metric_values"]) > 0


def test_c4_peak_rss_ignores_a_tick_that_reported_no_rss(tmp_path):
    """If the unmeasured tick is the one holding the true peak, coercing its null
    to zero moves the reported peak to a lower tick with no error."""
    records = [resource_sample(1.0, "opensearch", 1_000_000_000, None, i=1),
               resource_sample(2.0, "opensearch", 9_000_000_000, None, i=2),
               resource_sample(3.0, "opensearch", 2_000_000_000, None, i=3)]
    records[1]["rss_bytes"] = None
    assert plot_c4.peak_rss(records) == 2_000_000_000
    assert plot_c4.peak_total_rss(records) == 2_000_000_000


def test_c7_will_not_make_a_knee_of_a_rung_where_nothing_succeeded():
    """An engine shedding load rejects fast: the offered rate is still met, the
    percentiles are computed over an empty sample and reported as zero, and the
    rung then holds the lowest p99 at the highest rate on the ladder. Recorded as
    unsaturated by a producer that did not check the count, it would be read as
    the engine sustaining its top rate at 0 ms."""
    healthy = sweep_point(200, 199.5, 20.0, 1.0, False)
    all_errors = sweep_point(1_800, 1_799.0, 0.0, 0.0, False,
                             count=0, errors=107_940)
    knee = plot_c7.knee_point([healthy, all_errors], 50.0)
    assert knee is not None and knee["offered_qps"] == 200
    assert plot_c7.NOTHING_SUCCEEDED in plot_c7.rule_saturated(all_errors)


def test_c8_shows_the_median_the_observed_range_and_the_timeouts(tmp_path,
                                                                monkeypatch):
    probes = [freshness_probe(0, 0.8), freshness_probe(1, 1.0),
              freshness_probe(2, 3.0), freshness_probe(3, None, timed_out=True)]
    spec = series_set(tmp_path, "c8", "opensearch", "opensearch", [probes])
    output = os.path.join(str(tmp_path), "synthetic-c8.png")
    figure, extras = rendered(plot_c8, monkeypatch,
                              ["--config", spec, "--output", output],
                              "freshness_probe", plot_c8.run_median_lag)
    summary = extras["per_config"]["opensearch"]
    assert summary["median_lag_s"] == pytest.approx(1.0)
    assert summary["observed_range_s"]["max"] == pytest.approx(3.0)
    assert summary["probes_timed_out"] == 1
    assert "never became searchable" in all_text(figure)


def test_c8_records_the_poll_interval_that_bounds_its_resolution(tmp_path,
                                                                monkeypatch):
    probes = [freshness_probe(index, 1.0 + index / 10) for index in range(5)]
    spec = series_set(tmp_path, "c8", "opensearch", "opensearch", [probes])
    output = os.path.join(str(tmp_path), "synthetic-c8.png")
    _, extras = rendered(plot_c8, monkeypatch,
                         ["--config", spec, "--output", output],
                         "freshness_probe", plot_c8.run_median_lag)
    assert extras["per_config"]["opensearch"]["poll_interval_s"] == \
        pytest.approx(0.05)
    assert any("poll" in note for note in extras["notes"])


def mixed_spec(directory: Any, per_repetition: Sequence[tuple[str, int]]) -> str:
    """One artifact per repetition, each free to name its own corpus and size."""
    for index, (corpus, max_docs) in enumerate(per_repetition, start=1):
        write_series(directory, f"synthetic-c5-mixed-{index}.jsonl", "opensearch",
                     searches(200, 10.0), corpus=corpus, max_docs=max_docs)
    return f"mixed:{os.path.join(str(directory), 'synthetic-c5-mixed-*.jsonl')}"


def test_a_smoke_repetition_beside_a_full_one_refuses_to_plot(tmp_path):
    """A 20k --smoke artifact lands in data/ under the same name as a 270k
    campaign repetition; the median of the two describes neither."""
    spec = mixed_spec(tmp_path, [(SYNTHETIC_CORPUS, 270269),
                                 (SYNTHETIC_CORPUS, 20000)])
    with pytest.raises(SystemExit) as raised:
        plotlib.collect_configs([spec], "latency_op", plot_c5.run_search_p99)
    assert "max_docs" in str(raised.value)
    assert "synthetic-c5-mixed-2.jsonl" in str(raised.value)


def test_repetitions_of_two_different_corpora_refuse_to_plot(tmp_path):
    spec = mixed_spec(tmp_path, [(SYNTHETIC_CORPUS, 270269),
                                 ("data/enwiki.jsonl", 270269)])
    with pytest.raises(SystemExit) as raised:
        plotlib.collect_configs([spec], "latency_op", plot_c5.run_search_p99)
    assert "corpus" in str(raised.value)


def test_repetitions_that_agree_still_plot(tmp_path):
    spec = mixed_spec(tmp_path, [(SYNTHETIC_CORPUS, 270269),
                                 (SYNTHETIC_CORPUS, 270269)])
    assert plotlib.collect_configs([spec], "latency_op",
                                   plot_c5.run_search_p99)[0].repetitions == 2


def test_an_artifact_from_before_the_field_existed_is_not_a_disagreement(tmp_path):
    """The fifteen surviving ScyllaDB C1 series predate `max_docs` in the build
    header. Absent is not different."""
    spec = mixed_spec(tmp_path, [(SYNTHETIC_CORPUS, 270269),
                                 (SYNTHETIC_CORPUS, 0)])
    assert plotlib.collect_configs([spec], "latency_op",
                                   plot_c5.run_search_p99)[0].repetitions == 2


def thin_write_ops(count: int, ops_per_bucket: int, bucket_s: float,
                   latency_ms: float = 50.0) -> list[dict[str, Any]]:
    """Write operations paced so each bucket holds `ops_per_bucket` of them.

    This is the shape the laptop campaign actually produced: 2,000 docs/s
    offered at a batch size of 500 is 4 operations per second, so a 5 s bucket
    held 20 — against a floor of 100 for p99 and 1,000 for p999.
    """
    spacing = bucket_s / ops_per_bucket
    return [{**latency_op(index, latency_ms, op="bulk", n_docs=500),
             "t_intended_s": index * spacing}
            for index in range(count)]


def test_c3_refuses_a_run_whose_every_bucket_is_too_thin(tmp_path, monkeypatch):
    """The laptop campaign's C3 png was axes and a legend reading "0/28 buckets"
    six times, and it sat in the curated results tree looking like a chart. The
    per-bucket refusal was right; being silent about refusing *every* bucket was
    not."""
    records = thin_write_ops(541, ops_per_bucket=20, bucket_s=5.0)
    spec = series_set(tmp_path, "c3", "opensearch", "opensearch", [records])
    output = os.path.join(str(tmp_path), "c3.png")
    args = parsed(plot_c3, monkeypatch,
                  ["--config", spec, "--output", output, "--bucket-s", "5"])
    with pytest.raises(SystemExit):
        plotlib.emit(args, "latency_op", plot_c3.run_write_p99, plot_c3.plot)
    assert not os.path.exists(output), \
        "a chart with nothing on it must not reach the results tree"


def test_c3_refusal_states_the_rate_over_batch_arithmetic(tmp_path, monkeypatch):
    """The fix is not more documents — buckets per run scales with the corpus but
    operations per bucket is target_rate / batch_size, which does not. A message
    that does not say so sends the next campaign to re-run at 73x the corpus for
    the same empty chart."""
    records = thin_write_ops(541, ops_per_bucket=20, bucket_s=5.0)
    spec = series_set(tmp_path, "c3", "opensearch", "opensearch", [records],
                      target_rate_docs_per_s=2000.0, batch_size=500)
    args = parsed(plot_c3, monkeypatch,
                  ["--config", spec, "--output",
                   os.path.join(str(tmp_path), "c3.png"), "--bucket-s", "5"])
    with pytest.raises(SystemExit) as raised:
        plotlib.emit(args, "latency_op", plot_c3.run_write_p99, plot_c3.plot)
    message = str(raised.value)
    assert "operations per bucket" in message
    assert "batch 500" in message
    assert "--batch-size" in message


def test_c3_draws_when_the_buckets_can_carry_the_percentile(tmp_path, monkeypatch):
    records = thin_write_ops(2000, ops_per_bucket=1000, bucket_s=5.0)
    spec = series_set(tmp_path, "c3", "opensearch", "opensearch", [records])
    output = os.path.join(str(tmp_path), "c3.png")
    args = parsed(plot_c3, monkeypatch,
                  ["--config", spec, "--output", output, "--bucket-s", "5"])
    assert plotlib.emit(args, "latency_op", plot_c3.run_write_p99,
                        plot_c3.plot) == 0
    assert os.path.exists(output)


def test_c3_whole_run_percentiles_obey_the_floor_the_buckets_obey():
    """541 operations cannot carry p999 — the floor is 1,000 — yet the sidecar
    recorded one and FINDINGS quoted it. A percentile the harness refuses to
    draw must not be a number it publishes."""
    records = thin_write_ops(541, ops_per_bucket=20, bucket_s=5.0)
    buckets = plot_c3.bucketize(records, 5.0)
    summary = plot_c3.summary_for(records, buckets, 5.0, {99.0: 28, 99.9: 28})
    assert 99.0 in summary["whole_run"], "541 samples do carry p99"
    assert 99.9 not in summary["whole_run"], \
        "541 samples cannot carry p999; the floor is 1,000"
    assert "p99.9" in summary["whole_run_refused"]


def test_a_figure_with_no_drawn_data_is_not_written(tmp_path, monkeypatch):
    """Generic guard: every chart funnels through finish_figure, so the check
    that something was drawn belongs there and not in eight plot modules."""
    figure, axes = plotlib.plt.subplots(figsize=(8, 5))
    args = parsed(plot_c3, monkeypatch,
                  ["--config", "x:y", "--output",
                   os.path.join(str(tmp_path), "blank.png")])
    with pytest.raises(SystemExit):
        plotlib.finish_figure(figure, args, ())


def test_a_figure_with_bars_counts_as_drawn(tmp_path, monkeypatch):
    """C2, C4 and C6 draw bars, which are patches rather than lines; the guard
    must not reject them."""
    figure, axes = plotlib.plt.subplots(figsize=(8, 5))
    axes.bar([0, 1], [3.0, 4.0])
    output = os.path.join(str(tmp_path), "bars.png")
    args = parsed(plot_c3, monkeypatch, ["--config", "x:y", "--output", output])
    plotlib.finish_figure(figure, args, ())
    assert os.path.exists(output)
