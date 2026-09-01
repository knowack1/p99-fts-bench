"""Tests for the gate recorder.

The property that matters is that a *failed* gate survives in the artifact. A
gate that only prints to a terminal leaves behind a manifest indistinguishable
from a run nobody checked, and the chart README then says the gates were not
machine-recorded — which is the one claim a reviewer cannot verify.
"""
import json
import sys

import pytest

from ftsbench import gate_log


def record(monkeypatch: pytest.MonkeyPatch, manifest, name: str, status: str,
           observed: str = "") -> int:
    monkeypatch.setattr(sys, "argv", [
        "gate_log", "--manifest", str(manifest), "--name", name,
        "--status", status, "--observed", observed])
    return gate_log.main()


def gates_of(manifest) -> dict:
    return json.loads(manifest.read_text(encoding="utf-8"))["gates"]


def test_a_passing_gate_is_recorded_and_exits_zero(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"record": "run_manifest", "config": "opensearch"}))
    assert record(monkeypatch, manifest, "doc_count", "pass", "270269 of 270269") == 0
    entry = gates_of(manifest)["doc_count"]
    assert entry["status"] == "pass"
    assert entry["observed"] == "270269 of 270269"
    assert json.loads(manifest.read_text())["config"] == "opensearch"


def test_a_failing_gate_is_recorded_and_exits_nonzero(tmp_path, monkeypatch):
    """Recording and aborting are the same step on purpose: a caller cannot
    record a failure and carry on measuring."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"record": "run_manifest"}))
    assert record(monkeypatch, manifest, "index_complete", "fail",
                  "count=180000 status=SERVING, expected 270269") == 1
    entry = gates_of(manifest)["index_complete"]
    assert entry["status"] == "fail"
    assert "180000" in entry["observed"]


def test_several_gates_accumulate_in_one_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"record": "run_manifest"}))
    record(monkeypatch, manifest, "doc_count", "pass", "ok")
    record(monkeypatch, manifest, "not_oom_killed", "pass", "false 0")
    assert set(gates_of(manifest)) == {"doc_count", "not_oom_killed"}


def test_rerunning_a_gate_replaces_its_own_entry(tmp_path, monkeypatch):
    """Two opinions from the same gate in one manifest would leave a reader no
    way to know which one described the run that was kept."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"record": "run_manifest"}))
    record(monkeypatch, manifest, "doc_count", "fail", "count=1")
    record(monkeypatch, manifest, "doc_count", "pass", "count=270269")
    entry = gates_of(manifest)["doc_count"]
    assert entry["status"] == "pass" and entry["observed"] == "count=270269"


def test_a_missing_manifest_does_not_lose_the_gate(tmp_path, monkeypatch):
    """A repetition that failed before its manifest was written is exactly the
    one whose gate outcome matters most."""
    manifest = tmp_path / "nested" / "manifest.json"
    assert record(monkeypatch, manifest, "series_usable", "fail", "3 samples") == 1
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["gates"]["series_usable"]["status"] == "fail"
    assert "did not exist" in document["note"]


def test_an_unparseable_manifest_does_not_lose_the_gate(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{truncated by an OOM kill")
    assert record(monkeypatch, manifest, "not_oom_killed", "fail", "true 137") == 1
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["gates"]["not_oom_killed"]["observed"] == "true 137"
    assert "unparseable" in document["note"]


def test_results_tree_finds_what_gate_log_writes(tmp_path, monkeypatch):
    """The two sides agree on the key by construction rather than by comment."""
    from ftsbench import results_tree
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"record": "run_manifest"}))
    record(monkeypatch, manifest, "doc_count", "pass", "ok")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert gate_log.GATES_KEY in results_tree.GATE_KEYS
    assert any(document.get(key) for key in results_tree.GATE_KEYS)
