"""Archiving one configuration must not touch another's data.

Re-running the two OpenSearch configurations has to leave fifteen valid ScyllaDB
artifacts in place; a campaign-wide sweep would move them into data/superseded/
where no plot glob looks. And `c5-opensearch-*` matches
`c5-opensearch-refresh30-rare_term-1.jsonl`, which is the same prefix collision
that already summarized the refresh=30s repetitions twice.
"""
from pathlib import Path

import pytest

from ftsbench import archive_artifacts

CONFIGS = ("opensearch", "opensearch-refresh30", "scylla-bootstrap", "scylla-cdc")

ARTIFACTS = (
    "c1-opensearch-1.jsonl",
    "c4-opensearch-1.jsonl",
    "c5-opensearch-rare_term-1.jsonl",
    "c1-opensearch-refresh30-1.jsonl",
    "c5-opensearch-refresh30-rare_term-1.jsonl",
    "c7-opensearch-refresh30-1.json",
    "manifest-opensearch-1.json",
    "manifest-opensearch-refresh30-1.json",
    "c1-scylla-bootstrap-1.jsonl",
    "c1-scylla-cdc-1.jsonl",
    "c5-scylla-cdc-phrase-3.jsonl",
    "manifest-scylla-cdc-3.json",
)
UNRELATED = ("corpus.jsonl", "queries.json")


def populated(root: Path) -> Path:
    data = root / "data"
    logs = data / "campaign-logs"
    logs.mkdir(parents=True)
    for name in ARTIFACTS + UNRELATED:
        (data / name).write_text("{}\n", encoding="utf-8")
    for config in CONFIGS:
        (logs / f"{config}-1.log").write_text("log\n", encoding="utf-8")
    return data


def names_left(data: Path) -> set[str]:
    return {path.name for path in data.iterdir() if path.is_file()}


def test_archiving_opensearch_leaves_refresh30_and_scylladb_alone(tmp_path):
    data = populated(tmp_path)
    archive_artifacts.move_all(
        archive_artifacts.selected(data, ["opensearch"]), data,
        tmp_path / "superseded")
    remaining = names_left(data)
    assert "c1-opensearch-1.jsonl" not in remaining
    assert "c5-opensearch-rare_term-1.jsonl" not in remaining
    assert "manifest-opensearch-1.json" not in remaining
    assert "c1-opensearch-refresh30-1.jsonl" in remaining
    assert "c5-opensearch-refresh30-rare_term-1.jsonl" in remaining
    assert "c1-scylla-bootstrap-1.jsonl" in remaining
    assert "c1-scylla-cdc-1.jsonl" in remaining


def test_archiving_refresh30_leaves_the_plain_configuration_alone(tmp_path):
    data = populated(tmp_path)
    archive_artifacts.move_all(
        archive_artifacts.selected(data, ["opensearch-refresh30"]), data,
        tmp_path / "superseded")
    remaining = names_left(data)
    assert "c1-opensearch-refresh30-1.jsonl" not in remaining
    assert "c7-opensearch-refresh30-1.json" not in remaining
    assert "c1-opensearch-1.jsonl" in remaining
    assert "c5-opensearch-rare_term-1.jsonl" in remaining


def test_the_corpus_is_never_an_artifact(tmp_path):
    """data/corpus.jsonl and data/queries.json live beside the artifacts and are
    the inputs every repetition reads."""
    data = populated(tmp_path)
    selected = {path.name for path in archive_artifacts.selected(data, CONFIGS)}
    assert not selected & set(UNRELATED)


def test_repetition_logs_travel_with_their_configuration(tmp_path):
    data = populated(tmp_path)
    selected = {path.name for path in archive_artifacts.selected(data, ["scylla-cdc"])}
    assert "scylla-cdc-1.log" in selected
    assert "scylla-bootstrap-1.log" not in selected


def test_archived_files_keep_their_place_under_the_archive(tmp_path):
    """A log has to stay in campaign-logs/, or restoring an archived run means
    working out by hand which files were logs."""
    data = populated(tmp_path)
    archive = tmp_path / "superseded"
    archive_artifacts.move_all(archive_artifacts.selected(data, ["scylla-cdc"]),
                               data, archive)
    assert (archive / "c1-scylla-cdc-1.jsonl").is_file()
    assert (archive / "campaign-logs" / "scylla-cdc-1.log").is_file()


@pytest.mark.parametrize("filename,expected", [
    ("c4-opensearch-refresh30-1.jsonl", "opensearch-refresh30"),
    ("c4-opensearch-1.jsonl", "opensearch"),
    ("c1-scylla-cdc-2.jsonl", "scylla-cdc"),
    ("c1-scylla-bootstrap-2.jsonl", "scylla-bootstrap"),
    ("manifest-opensearch-refresh30-5.json", "opensearch-refresh30"),
    ("corpus.jsonl", None),
])
def test_the_longest_matching_configuration_wins(filename, expected):
    assert archive_artifacts.artifact_config(filename, CONFIGS) == expected


def test_the_configuration_list_is_the_campaign_s():
    """Parsing depends on knowing every configuration that exists, so a fifth one
    added to the campaign and not here would be archived as one of the other
    four."""
    from ftsbench import run_manifest
    assert set(archive_artifacts.KNOWN_CONFIGS) == set(run_manifest.CONFIGS)


def test_a_misspelled_configuration_is_refused_not_ignored(tmp_path):
    """Archiving nothing and reporting success is indistinguishable from a
    directory that was already clean."""
    data = populated(tmp_path)
    with pytest.raises(SystemExit):
        archive_artifacts.selected(data, ["opensearch-refresh-30"])


def test_only_the_repetitions_being_re_run_are_archived(tmp_path):
    """Re-running scylla-cdc repetitions 1 and 3 must leave 2, 4 and 5 in place:
    their query and freshness logs are valid measurements, and the re-run is
    never going to rewrite them."""
    data = populated(tmp_path)
    archive_artifacts.move_all(
        archive_artifacts.selected(data, ["scylla-cdc"], reps=[1]), data,
        tmp_path / "superseded")
    remaining = names_left(data)
    assert "c1-scylla-cdc-1.jsonl" not in remaining
    assert "c5-scylla-cdc-phrase-3.jsonl" in remaining
    assert "manifest-scylla-cdc-3.json" in remaining


def test_a_repetition_log_is_archived_with_its_repetition(tmp_path):
    data = populated(tmp_path)
    archive_artifacts.move_all(
        archive_artifacts.selected(data, ["scylla-cdc"], reps=[2]), data,
        tmp_path / "superseded")
    assert (data / "campaign-logs" / "scylla-cdc-1.log").exists()


def test_no_repetition_filter_still_archives_the_whole_configuration(tmp_path):
    data = populated(tmp_path)
    archive_artifacts.move_all(
        archive_artifacts.selected(data, ["scylla-cdc"]), data,
        tmp_path / "superseded")
    remaining = names_left(data)
    assert "c1-scylla-cdc-1.jsonl" not in remaining
    assert "c5-scylla-cdc-phrase-3.jsonl" not in remaining


@pytest.mark.parametrize("filename,expected", [
    ("c1-opensearch-4.jsonl", 4),
    ("c5-scylla-cdc-bool_and-2.jsonl", 2),
    ("manifest-opensearch-refresh30-10.json", 10),
    ("scylla-cdc-3.log", 3),
    ("corpus.jsonl", None),
    ("c6-scylla-cdc.jsonl", None),
])
def test_the_repetition_is_read_from_the_end_of_the_name(filename, expected):
    assert archive_artifacts.artifact_rep(filename) == expected


def test_the_c3_manifest_is_archived_with_its_repetition():
    """C3 writes its own manifest (`manifest-c3-<config>-<rep>.json`, Makefile
    C3_OS_MANIFEST). Parsed only against the `manifest-` prefix it belongs to no
    configuration, so re-running a repetition left its C3 gate records behind as
    an orphan beside the fresh ones — and results_tree globs
    `manifest-c3-<config>-*`, so the stale file would be read as part of the new
    run."""
    from ftsbench import archive_artifacts
    assert archive_artifacts.artifact_config(
        "manifest-c3-opensearch-1.json") == "opensearch"
    assert archive_artifacts.artifact_config(
        "manifest-c3-opensearch-refresh30-2.json") == "opensearch-refresh30"
    assert archive_artifacts.artifact_config(
        "manifest-c3-scylla-cdc-5.json") == "scylla-cdc"


def test_the_run_manifest_is_still_told_apart_from_the_c3_manifest():
    from ftsbench import archive_artifacts
    assert archive_artifacts.artifact_config(
        "manifest-opensearch-1.json") == "opensearch"


def test_every_manifest_the_makefile_writes_can_be_archived():
    """The prefixes are a hand-maintained list; the Makefile is what names the
    files. A shape added there and not here is silently unarchivable."""
    import re
    from pathlib import Path

    from ftsbench import archive_artifacts

    makefile = Path(__file__).resolve().parent.parent / "Makefile"
    named = re.findall(r"\$\(DATA_DIR\)/(manifest[a-z0-9$()_-]*)-\$\(REP\)\.json",
                       makefile.read_text())
    shapes = {re.sub(r"-(\$\([A-Z_]+\)|scylla-bootstrap|scylla-cdc|"
                     r"opensearch-refresh30|opensearch)$", "", shape)
              for shape in named}
    assert shapes, "no manifest paths found in the Makefile"
    unparsable = [shape for shape in shapes
                  if archive_artifacts.artifact_config(
                      f"{shape}-opensearch-1.json") is None]
    assert not unparsable, f"unarchivable manifest shapes: {unparsable}"
