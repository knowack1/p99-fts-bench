"""What a run offered the engine has to be readable from the run's own files.

results/aws-enwiki-2026-09/S28-RETRACTION.md is the precedent: that header
recorded no concurrency, so when a defect was found the artifacts could not say
what shape had produced them and the whole chart had to be withdrawn. The
build-rate matrix makes the same omission cheaper to commit — batch size is
swept on OpenSearch and does not exist on ScyllaDB, whose operation is always
one prepared INSERT, so two series at the same x are two different write shapes
and nothing on the axis says so.

Two records carry it, deliberately: the series header and the run manifest. A
manifest that disagrees with its series is the only way to catch a sweep that
passed one batch size to make and another to the loader.

The other half of the contract is silence. Every series measured before these
flags existed has to keep parsing as the same header it was, so an unset flag
must be absent rather than recorded as a zero or a default — a number there
would read as a measured fact about a run nobody measured that way.
"""
import json
import re
from pathlib import Path

import pytest

from ftsbench import build_monitor, run_manifest, scylla_load

SCHEMAS_MD = Path(__file__).resolve().parent.parent / "SCHEMAS.md"

SHAPE_FIELDS = ("batch_size",)


class StubSampler:
    def version(self) -> str:
        return "3.8.0"


class Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        pass


def series_header(*flags: str) -> dict:
    """Through the real parser and the real writer, because the omission rule
    lives in the seam between them and a hand-built Namespace would skip it."""
    argv = ["--engine", "opensearch", "--output", "-", *flags]
    out = Capture()
    build_monitor.write_header(out, build_monitor.parse_args_from(argv),
                               StubSampler())
    return json.loads("".join(out.lines))


def manifest(monkeypatch, *flags: str) -> dict:
    monkeypatch.setattr(run_manifest, "probe_opensearch",
                        lambda url: {"reachable": False})
    argv = ["run_manifest", "--output", "-", "--config", "opensearch",
            "--rep", "1", "--label", "unit test", *flags]
    monkeypatch.setattr("sys.argv", argv)
    return run_manifest.build_manifest(run_manifest.parse_args())


def test_a_series_header_records_the_batch_size_it_was_given():
    assert series_header("--batch-size", "512")["batch_size"] == 512


@pytest.mark.parametrize("field", SHAPE_FIELDS)
def test_a_series_header_omits_a_shape_nobody_named(field):
    """Every series already on disk was written without these flags. If an
    unset flag landed as 0 those headers would now differ from the ones beside
    them by a value that was never measured."""
    assert field not in series_header()


def test_the_retired_rows_in_flight_field_is_not_resurrected():
    """`rows_in_flight` was a ScyllaDB-only bound that duplicated
    --concurrency. It is gone from the producers, and a header that started
    carrying it again would put a knob back on an axis that has none."""
    assert "rows_in_flight" not in series_header("--batch-size", "128")


def test_a_manifest_records_every_shape_field_it_was_given(monkeypatch):
    recorded = manifest(monkeypatch, "--batch-size", "1")
    assert [recorded[field] for field in SHAPE_FIELDS] == [1]


@pytest.mark.parametrize("field", SHAPE_FIELDS)
def test_a_manifest_omits_a_shape_nobody_named(monkeypatch, field):
    assert field not in manifest(monkeypatch)


def test_an_explicit_zero_survives_where_an_absent_flag_disappears(monkeypatch):
    """A named 0 is a measured decision and not an absence. Only an unset flag
    may vanish."""
    recorded = manifest(monkeypatch, "--batch-size", "0")
    assert recorded["batch_size"] == 0


def scylla_args(monkeypatch, *flags: str):
    argv = ["scylla_load", "--corpus", "data/corpus.jsonl",
            "--concurrency", "8", *flags]
    monkeypatch.setattr("sys.argv", argv)
    return scylla_load.parse_args()


@pytest.mark.parametrize("flag,value", [("--batch-size", "500"),
                                        ("--rows-in-flight", "64"),
                                        ("--unlogged-batch-rows", "30")])
def test_the_scylla_loader_refuses_a_shape_it_cannot_have(monkeypatch, capsys,
                                                          flag, value):
    """One ScyllaDB operation is one prepared INSERT. Accepting any of these and
    ignoring it would put a batch size in the label and the manifest that the
    run never had — the S28 failure: complete, plausible artifacts describing a
    shape nobody ran."""
    with pytest.raises(SystemExit) as refused:
        scylla_args(monkeypatch, flag, value)
    assert refused.value.code == 2
    assert flag in capsys.readouterr().err


def test_the_scylla_loader_still_takes_the_knob_it_does_have(monkeypatch):
    """--concurrency is the whole write-side axis on this engine."""
    assert scylla_args(monkeypatch).concurrency == 8


def test_a_scylla_series_says_one_document_per_operation(monkeypatch):
    """Absence would leave a reader to infer the shape. The header states it:
    the batch axis is OpenSearch's, and ScyllaDB's is fixed at one."""
    from ftsbench import load_driver
    loader = load_driver.EngineLoader(
        name="scylla", engine="scylladb", op_kind="insert",
        engine_version="test", docs_per_operation=scylla_load.DOCS_PER_OPERATION,
        encode=lambda batch: batch, send=None)
    args = scylla_args(monkeypatch)
    args.label, args.cache_state, args.max_docs = "", "unspecified", 0
    args.target_rate = 0.0
    header = load_driver._header(args, loader)
    assert header["batch_size"] == 1
    assert "rows_in_flight" not in header


def test_the_shape_keys_are_the_ones_the_results_tree_already_looks_for():
    """ftsbench/results_tree.py prints UNRECORDED for a run whose tuning it
    cannot find. Renaming a field on the producing side alone would put that
    word back over runs that did record their shape."""
    from ftsbench import results_tree
    assert "batch_size" in results_tree.TUNING_HEADER_KEYS


def test_the_manifest_keeps_the_shape_beside_the_rest_of_the_run(monkeypatch):
    """A reader scanning a manifest for what the run offered should not have to
    hunt: the shape belongs with max_docs and the series it describes, not after
    the host block."""
    keys = list(manifest(monkeypatch, "--batch-size", "512"))
    assert keys.index("max_docs") < keys.index("batch_size") < keys.index("series")


def test_a_legacy_series_header_still_carries_its_original_fields():
    """The omission must be an omission and nothing more — the fields a chart
    reads from a pre-matrix header have to survive unchanged."""
    header = series_header()
    for field in ("record", "producer", "engine", "engine_version", "label",
                  "cache_state", "corpus", "max_docs", "interval_s",
                  "idle_timeout_s", "max_seconds", "settle_timeout_s"):
        assert field in header, f"{field} disappeared from the header"


def documented_header_fields() -> tuple[str, ...]:
    """Read out of SCHEMAS.md rather than restated, the way
    tests/test_schema_conformance.py reads the latency_op field list: a test
    that hardcodes the list only proves it agrees with itself."""
    text = SCHEMAS_MD.read_text(encoding="utf-8")
    heading = re.search(r"^## Header.*$", text, flags=re.MULTILINE)
    assert heading, "SCHEMAS.md no longer documents the header"
    block = re.search(r"```json\n(.*?)\n```", text[heading.end():],
                      flags=re.DOTALL)
    return tuple(json.loads(block.group(1)))


@pytest.mark.parametrize("field", ["batch_size"])
def test_the_documented_header_shows_the_shape_a_series_records(field):
    """SCHEMAS.md is the contract every producer is held to. A field a producer
    writes and the document does not show is a field the next reader has no
    reason to expect."""
    assert field in documented_header_fields()
