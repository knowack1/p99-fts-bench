"""Tests for the results-tree generator.

The fixtures are synthetic sidecars, headers and manifests, named so that they
cannot be mistaken for measurements. What matters here is not formatting but
two refusals: a field absent from the artifacts must appear as `UNRECORDED`
rather than being inferred, and a hand-written document must never be
overwritten by a generated one.
"""
import json
import os
import sys
from typing import Any, Sequence

import pytest

from ftsbench import results_tree

SYNTHETIC_LABEL = "SYNTHETIC pytest fixture — invented values, not a measurement"


def write_json(path: str, document: dict[str, Any]) -> str:
    with open(path, "w", encoding="utf-8") as out:
        json.dump(document, out)
    return path


def sidecar_config() -> dict[str, Any]:
    """One config as `plotlib.config_sidecar` writes it, with an unrounded float
    so the README's own formatting is observable."""
    return {
        "chosen_file": "data/synthetic-c3-opensearch-2.jsonl",
        "repetitions": 3,
        "repetition_files": ["data/synthetic-c3-opensearch-1.jsonl"],
        "metric": "write_p99_ms",
        "repetition_metric_values": [70.06027999999998, 72.0, 77.0],
        "repetition_metric_spread": {"min": 70.06027999999998, "max": 77.0,
                                     "median": 72.0},
        "engine": "opensearch", "engine_version": "0.0.0-synthetic",
        "label": SYNTHETIC_LABEL, "cache_state": "cold",
        "corpus": "data/synthetic-corpus.jsonl", "max_docs": 1000,
        "git_commit": "0000000", "errors": 0,
        "host": {"hostname": "synthetic-host", "cpu_count": 4}, "env": {},
    }


def sidecar_document(**extra: Any) -> dict[str, Any]:
    return {
        "chart": "C3", "title": "Synthetic write latency", "subtitle": "synthetic",
        "png": "synthetic-c3.png",
        "command": "python3 -m ftsbench.plot_c3 --synthetic",
        "claim": "synthetic claim", "claim_status": "synthetic status",
        "confidence_tier": "Tier 3 — synthetic", "aws_delta": "synthetic delta",
        "preliminary": True, "preliminary_stamp": "PRELIMINARY — synthetic",
        "write_path_disclosure": "synthetic disclosure",
        "run_selection": "median repetition", "chart_notes": ["synthetic note"],
        "x_axis": "synthetic x", "y_axis": "synthetic y",
        "configs": {"opensearch": sidecar_config()},
        **extra,
    }


def manifest_document(**extra: Any) -> dict[str, Any]:
    return {
        "record": "run_manifest", "schema_version": 1,
        "timestamp": "2000-01-01T00:00:00Z", "config": "opensearch",
        "repetition": 1, "label": SYNTHETIC_LABEL, "cache_state": "cold",
        "corpus": "data/synthetic-corpus.jsonl", "max_docs": 1000,
        "series": "data/synthetic-c3-opensearch-1.jsonl",
        "images": {"opensearch_image": "synthetic/opensearch:0.0.0"},
        "engines": {"opensearch": {"reachable": True, "version": "0.0.0-synthetic"}},
        "commands": ["make synthetic-first", "make synthetic-second"],
        "host": {"hostname": "synthetic-host"},
        **extra,
    }


def write_series_header(path: str, **extra: Any) -> str:
    header = {"record": "header", "schema_version": 1, "engine": "opensearch",
              "engine_version": "0.0.0-synthetic", "label": SYNTHETIC_LABEL,
              "corpus": "data/synthetic-corpus.jsonl",
              "host": {"hostname": "synthetic-host"}, **extra}
    with open(path, "w", encoding="utf-8") as out:
        out.write(json.dumps(header) + "\n")
        out.write(json.dumps({"record": "latency_op", "latency_ms": 1.0}) + "\n")
    return path


def fixtures(directory: Any, sidecar: dict[str, Any] | None = None,
             manifest: dict[str, Any] | None = None,
             **header: Any) -> dict[str, str]:
    root = str(directory)
    paths = {"sidecar": os.path.join(root, "synthetic-c3.json"),
             "png": os.path.join(root, "synthetic-c3.png"),
             "series": os.path.join(root, "synthetic-c3-opensearch-1.jsonl"),
             "manifest": os.path.join(root, "synthetic-manifest-opensearch-1.json")}
    write_json(paths["sidecar"], sidecar if sidecar is not None
               else sidecar_document())
    write_json(paths["manifest"], manifest if manifest is not None
               else manifest_document())
    write_series_header(paths["series"], **header)
    with open(paths["png"], "wb") as out:
        out.write(b"\x89PNG synthetic placeholder")
    return paths


def chart_spec(paths: dict[str, str], chart_id: str = "C3") -> str:
    return (f"id={chart_id},sidecar={paths['sidecar']},png={paths['png']},"
            f"artifacts={paths['series']},manifests={paths['manifest']}")


def generate(monkeypatch: pytest.MonkeyPatch, root: Any, run_name: str,
             specs: Sequence[str], *flags: str) -> int:
    argv = ["synthetic-test", "--run-name", run_name, "--results-root", str(root)]
    for spec in specs:
        argv.extend(["--chart", spec])
    monkeypatch.setattr(sys, "argv", [*argv, *flags])
    return results_tree.main()


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def chart_readme(root: Any, run_name: str, directory: str) -> str:
    return read(os.path.join(str(root), run_name, directory, "README.md"))


def test_a_field_missing_from_the_sidecar_is_unrecorded_not_invented(tmp_path,
                                                                    monkeypatch):
    thin = sidecar_document()
    del thin["confidence_tier"]
    del thin["aws_delta"]
    paths = fixtures(tmp_path, sidecar=thin)
    root = tmp_path / "results"
    assert generate(monkeypatch, root, "synthetic-run",
                    [chart_spec(paths)]) == 0
    readme = chart_readme(root, "synthetic-run", "c3-write-tail-latency")
    assert f"Confidence: {results_tree.UNRECORDED}" in readme
    assert readme.count(results_tree.UNRECORDED) >= 2
    assert "synthetic delta" not in readme


def test_a_missing_sidecar_produces_a_write_up_that_says_so(tmp_path, monkeypatch):
    root = tmp_path / "results"
    spec = f"id=C7,sidecar={tmp_path}/synthetic-absent.json"
    assert generate(monkeypatch, root, "synthetic-run", [spec]) == 0
    readme = chart_readme(root, "synthetic-run", "c7-qps-sweep")
    assert "Incomplete" in readme
    assert f"# C7 — {results_tree.UNRECORDED}" in readme


def test_the_commands_come_from_the_manifests_in_recorded_order(tmp_path,
                                                               monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    readme = chart_readme(root, "synthetic-run", "c3-write-tail-latency")
    assert readme.index("make synthetic-first") < readme.index("make synthetic-second")
    assert os.path.basename(paths["manifest"]) in readme


def test_the_plot_command_comes_from_the_sidecar(tmp_path, monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    readme = chart_readme(root, "synthetic-run", "c3-write-tail-latency")
    assert "python3 -m ftsbench.plot_c3 --synthetic" in readme


def test_gates_are_unrecorded_when_no_artifact_records_them(tmp_path, monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    readme = chart_readme(root, "synthetic-run", "c3-write-tail-latency")
    gates = readme.split("## Gates that passed")[1].split("##")[0]
    assert results_tree.UNRECORDED in gates
    assert "gates_passed" in gates


def test_gates_are_reported_when_a_manifest_records_them(tmp_path, monkeypatch):
    paths = fixtures(tmp_path, manifest=manifest_document(
        gates=["synthetic doc-count gate", "synthetic no-OOM gate"]))
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    gates = chart_readme(root, "synthetic-run",
                         "c3-write-tail-latency").split("## Gates")[1]
    assert "synthetic doc-count gate" in gates
    assert "synthetic no-OOM gate" in gates


def test_tuning_is_read_out_of_the_series_headers(tmp_path, monkeypatch):
    paths = fixtures(tmp_path, refresh_interval="30s", batch_size=500,
                     concurrency=4)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    readme = chart_readme(root, "synthetic-run", "c3-write-tail-latency")
    assert "refresh_interval=30s" in readme
    assert "concurrency=4" in readme


def test_the_per_repetition_spread_is_reported_without_binary_noise(tmp_path,
                                                                   monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    readme = chart_readme(root, "synthetic-run", "c3-write-tail-latency")
    assert "70.0603" in readme
    assert "70.06027999999998" not in readme


def test_a_hand_written_index_is_never_overwritten(tmp_path, monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    run_directory = root / "synthetic-run"
    os.makedirs(run_directory)
    hand_written = run_directory / "README.md"
    hand_written.write_text("# Hand-written by another workstream\n",
                            encoding="utf-8")
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    assert hand_written.read_text(encoding="utf-8") == \
        "# Hand-written by another workstream\n"
    assert (run_directory / "README.generated.md").exists()


def test_a_previously_generated_file_is_replaced_in_place(tmp_path, monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    run_directory = root / "synthetic-run"
    assert not (run_directory / "README.generated.md").exists()
    assert results_tree.GENERATED_MARKER.split("—")[0].strip() in \
        read(str(run_directory / "README.md"))


def test_the_chart_directory_and_its_copies_follow_the_planned_layout(tmp_path,
                                                                     monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    directory = root / "synthetic-run" / "c3-write-tail-latency"
    assert (directory / "README.md").exists()
    assert (directory / os.path.basename(paths["png"])).exists()
    assert (directory / os.path.basename(paths["sidecar"])).exists()


def test_copy_raw_puts_the_series_and_manifests_under_raw(tmp_path, monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)], "--copy-raw")
    raw = root / "synthetic-run" / "c3-write-tail-latency" / "raw"
    assert (raw / os.path.basename(paths["series"])).exists()
    assert (raw / os.path.basename(paths["manifest"])).exists()


def test_raw_is_not_copied_unless_asked(tmp_path, monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    assert not (root / "synthetic-run" / "c3-write-tail-latency" / "raw").exists()


def test_the_index_lists_every_chart_with_its_tier_and_status(tmp_path,
                                                              monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    absent = f"id=C8,sidecar={tmp_path}/synthetic-absent.json"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths), absent])
    index = read(str(root / "synthetic-run" / "README.md"))
    assert "c3-write-tail-latency/README.md" in index
    assert "Tier 3 — synthetic" in index
    assert "c8-freshness/README.md" in index
    assert "incomplete" in index


def test_provenance_reports_the_host_recorded_in_the_series_header(tmp_path,
                                                                  monkeypatch):
    paths = fixtures(tmp_path)
    root = tmp_path / "results"
    generate(monkeypatch, root, "synthetic-run", [chart_spec(paths)])
    provenance = chart_readme(root, "synthetic-run",
                              "c3-write-tail-latency").split("## Provenance")[1]
    assert "synthetic-host" in provenance


def test_a_chart_spec_needs_an_id():
    with pytest.raises(ValueError):
        results_tree.parse_chart_spec("slug=only")


def test_a_chart_spec_rejects_a_key_it_does_not_understand():
    with pytest.raises(ValueError):
        results_tree.parse_chart_spec("id=C3,sidcar=typo.json")


def test_a_chart_spec_rejects_an_id_that_is_not_a_chart_number():
    with pytest.raises(ValueError):
        results_tree.parse_chart_spec("id=write-latency")


def test_a_chart_spec_takes_the_planned_slug_when_none_is_given():
    assert results_tree.parse_chart_spec("id=c6").directory == "c6-query-matrix"


def test_an_explicit_slug_overrides_the_planned_one():
    spec = results_tree.parse_chart_spec("id=C6,slug=synthetic-slug")
    assert spec.directory == "c6-synthetic-slug"


def test_a_bad_chart_spec_is_an_error_exit_not_a_traceback(tmp_path, monkeypatch):
    assert generate(monkeypatch, tmp_path / "results", "synthetic-run",
                    ["slug=no-id"]) == 2


def blank_chart_sidecar() -> dict[str, Any]:
    """The shape C3 actually produced: every bucket refused for want of samples,
    so no series was drawn."""
    config = {**sidecar_config(), "buckets": 28,
              "buckets_without_support": {"p99": 28, "p99.9": 28}}
    return sidecar_document(configs={"opensearch": config})


def test_a_chart_that_drew_nothing_says_so_at_the_top_of_its_readme(tmp_path,
                                                                   monkeypatch):
    """C3's write-up described a chart, its claim and its assessment for four
    days without mentioning that the png was blank. The reader of a generated
    README cannot be expected to reconstruct that from a bucket count."""
    paths = fixtures(tmp_path, sidecar=blank_chart_sidecar())
    assert generate(monkeypatch, tmp_path, "synthetic-run",
                    [chart_spec(paths)]) == 0
    body = chart_readme(tmp_path, "synthetic-run", "c3-write-tail-latency")
    banner = body.split("## What this chart claims")[0]
    assert "drew nothing" in banner
    assert "opensearch" in banner


def test_a_chart_with_supported_buckets_carries_no_blank_banner(tmp_path,
                                                                monkeypatch):
    config = {**sidecar_config(), "buckets": 28,
              "buckets_without_support": {"p99": 0, "p99.9": 3}}
    paths = fixtures(tmp_path, sidecar=sidecar_document(
        configs={"opensearch": config}))
    assert generate(monkeypatch, tmp_path, "synthetic-run",
                    [chart_spec(paths)]) == 0
    body = chart_readme(tmp_path, "synthetic-run", "c3-write-tail-latency")
    assert "drew nothing" not in body
