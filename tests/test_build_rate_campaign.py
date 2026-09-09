"""The roster: which runs the build-rate campaign intends, and what is left.

The table in tools/build_rate_campaign.sh is the campaign's intent, and three
things about it can be wrong in ways that are invisible until fleet time is
spent: an arm that no longer resolves in the registry, a row whose point count
does not match the decisions in BUILD-RATE-MATRIX-PLAN.md, and a done-count
that reads a set-aside point as a measured one.

The ladder is stubbed — what is under test is the roster, not the loop it
drives — using the same replace-one-file-in-a-symlink-tree pattern as
tests/test_sweep_script.py's `head_tree`.
"""
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from ftsbench import target

BENCH_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BENCH_DIR / "tools" / "build_rate_campaign.sh"

LADDER_STUB = r"""#!/usr/bin/env bash
# Records the arm, the reps and every knob the roster set for this row.
{
  printf 'row arm=%q reps=%q OUT_DIR=%q LADDER=%q BATCHES=%q SWEEP_DOCS=%q WARMUP=%q WORKERS=%q\n' \
    "$1" "${2:-}" "${OUT_DIR:-}" "${LADDER:-}" "${BATCHES:-}" \
    "${SWEEP_DOCS:-}" "${WARMUP:-}" "${WORKERS:-}"
} >> "$LADDER_LOG"
exit "${LADDER_EXIT:-0}"
"""


def stub_tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "tools").mkdir(parents=True)
    for entry in BENCH_DIR.iterdir():
        if entry.name != "tools":
            (root / entry.name).symlink_to(entry)
    for entry in (BENCH_DIR / "tools").iterdir():
        if entry.name != "sweep_build_rate.sh":
            (root / "tools" / entry.name).symlink_to(entry)
    ladder = root / "tools" / "sweep_build_rate.sh"
    ladder.write_text(LADDER_STUB, encoding="utf-8")
    ladder.chmod(0o755)
    return root


def run_campaign(tmp_path: Path, *args: str, root: Path | None = None,
                 env: dict[str, str] | None = None):
    tree = stub_tree(tmp_path)
    ladder_log = tmp_path / "ladder.log"
    ladder_log.touch()
    environment = {
        **os.environ,
        "LADDER_LOG": str(ladder_log),
        "ROOT": str(root or (tmp_path / "artifacts")),
        "PYTHON": str(BENCH_DIR / ".venv" / "bin" / "python3"),
        **(env or {}),
    }
    result = subprocess.run(["bash", str(tree / "tools" / "build_rate_campaign.sh"),
                             *args],
                            cwd=tree, capture_output=True, text=True,
                            env=environment, timeout=300)
    result.rows = [shlex.split(line)[1:] for line in
                   ladder_log.read_text(encoding="utf-8").splitlines()
                   if line.startswith("row ")]
    return result


def campaign_rows() -> list[dict[str, str]]:
    """The roster's own table, parsed from the script source.

    The rendered listing cannot be split on whitespace — the rungs and the
    batch levels are both space-separated lists — and the table is the thing
    under test anyway: it is where the campaign's intent is written down.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("CAMPAIGN=(", 1)[1].split("\n)", 1)[0]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        fields = [field.strip() for field in line.strip('"').split("|")]
        rows.append(dict(zip(("id", "group", "arm", "ladder", "batches",
                              "reps", "workers"), fields)))
    return rows


def row_field(row: list[str], name: str) -> str:
    for token in row:
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    raise AssertionError(f"{name} not in {row}")


def listing(tmp_path: Path, root: Path | None = None) -> str:
    result = run_campaign(tmp_path, "list", root=root)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def series_for(root: Path, group: str, config: str, concurrency: int, rep: int,
               batch: int | None = None) -> Path:
    directory = root / group if batch is None else root / group / f"b{batch}"
    return directory / f"c1-{config}-c{concurrency}-{rep}.jsonl"


def test_the_script_still_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_every_row_names_an_arm_the_registry_knows():
    """A flag the registry does not carry fails when the row runs, which on a
    serialized campaign is after the stack is up and an hour is spent."""
    rows = campaign_rows()
    assert rows, "the roster table is empty"
    known = {arm.flag for arm in target.TARGETS}
    named = {row["arm"] for row in rows}
    assert named <= known, f"not in ftsbench/target.py: {named - known}"


def test_the_roster_runs_the_five_arms_of_the_knob_matrix():
    """BUILD-RATE-MATRIX-PLAN.md's run table: R1 prices the writer buffer, R2
    adds parity, R3 adds the slow cadence, R4/R5 are the OpenSearch pair at the
    matching refresh cadences. A missing arm is a knob delta nobody measured."""
    laddered = {row["arm"] for row in campaign_rows() if row["group"] == "knobs"}
    assert laddered == {
        "--scylladb-cdc-buf15", "--scylladb-cdc-buf376",
        "--scylladb-cdc-buf376-commit30", "--opensearch-ram-nostore-refresh3",
        "--opensearch-ram-nostore-refresh30",
    }, laddered


def test_the_batch_axis_is_opensearch_only():
    """On OpenSearch a batch is a wire batch the engine sees. On ScyllaDB every
    row is its own prepared statement, so a level would be a dispatch window
    inside the client and the curve would measure ftsbench. The ladder refuses
    such a row outright; the roster must not write one."""
    for row in campaign_rows():
        if "scylladb" in row["arm"]:
            assert row["batches"] == "", f"{row['id']} sweeps a ScyllaDB batch axis"


def test_a_row_plans_rungs_times_levels_times_reps_points(tmp_path):
    """The point count is what the fleet is billed for, so it is stated rather
    than discovered: 7 rungs x 3 reps for a knob ladder, 5 levels x 3 reps for a
    batch measurement, 5 levels x 1 rep for a pin probe."""
    rows = {line.split()[0]: line.split() for line in listing(tmp_path).splitlines()
            if line.startswith("  R")}
    assert rows["R1"][-2:] == ["21", "0"], rows["R1"]
    assert rows["R4b"][-2:] == ["15", "0"], rows["R4b"]
    assert rows["R4p"][-2:] == ["5", "0"], rows["R4p"]


def test_a_measured_point_is_counted_and_a_set_aside_one_is_not(tmp_path):
    """`list` is only useful mid-campaign if "done" means measured. A point the
    completeness gate moved aside carries a .failed suffix precisely because no
    summariser may read it, and counting it would hide work still owed."""
    root = tmp_path / "artifacts"
    (root / "knobs").mkdir(parents=True)
    series_for(root, "knobs", "scylla-cdc-buf15", 4, 1).touch()
    aside = series_for(root, "knobs", "scylla-cdc-buf15", 8, 1)
    aside.with_suffix(".jsonl.failed").touch()
    row = [line.split() for line in listing(tmp_path, root=root).splitlines()
           if line.startswith("  R1 ")][0]
    assert row[-2:] == ["21", "1"], row


def test_run_hands_each_row_its_own_knobs(tmp_path):
    """Every difference between two rows has to arrive as a knob on the ladder:
    a row that inherited another's LADDER or BATCHES would produce a complete,
    plausible, wrongly-labelled curve."""
    result = run_campaign(tmp_path, "run", "R1", "R4p")
    assert result.returncode == 0, result.stderr[-2000:]
    assert len(result.rows) == 2, result.rows
    knobs, pin = result.rows
    assert row_field(knobs, "arm") == "--scylladb-cdc-buf15"
    assert row_field(knobs, "LADDER") == "4 8 16 32 64 96 128"
    assert row_field(knobs, "BATCHES") == ""
    assert row_field(knobs, "reps") == "3"
    assert row_field(pin, "arm") == "--opensearch-ram-nostore-refresh3"
    assert row_field(pin, "LADDER") == "16"
    assert row_field(pin, "BATCHES") == "16 64 128 256 512"
    assert row_field(pin, "reps") == "1"


def test_the_two_shapes_never_share_a_directory(tmp_path):
    """A concurrency ladder and a batch axis are different trees: the
    summariser reads a directory as one shape, and plot_batch_ceiling refuses a
    tree that holds a point from another level."""
    result = run_campaign(tmp_path, "run", "R1", "R4b")
    groups = {row_field(row, "OUT_DIR") for row in result.rows}
    assert len(groups) == 2, groups
    assert any(path.endswith("/knobs") for path in groups)
    assert any(path.endswith("/batch") for path in groups)


def test_every_row_runs_at_the_locked_cap_and_keeps_its_warm_up(tmp_path):
    """1,000,000 documents per point and one discarded warm-up per invocation,
    both from "Decisions locked". The warm-up is what makes the median of 3
    safe: the median of 3 is the middle value, which one cold repetition can
    move."""
    result = run_campaign(tmp_path, "run")
    assert result.rows, result.stderr[-2000:]
    for row in result.rows:
        assert row_field(row, "SWEEP_DOCS") == "1000000", row
        assert row_field(row, "WARMUP") == "1", row


def test_a_complete_row_is_not_run_again(tmp_path):
    """Fleet time is the scarce resource: a re-entered campaign must resume
    rather than re-measure what is already on disk."""
    root = tmp_path / "artifacts"
    (root / "batch").mkdir(parents=True)
    for batch in (16, 64, 128, 256, 512):
        (root / "batch" / f"b{batch}").mkdir()
        series_for(root, "batch", "opensearch-ramindex", 16, 1, batch).touch()
    result = run_campaign(tmp_path, "run", "R4p", root=root)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.rows == [], "a complete row was re-run"
    assert "already complete" in result.stderr


def test_a_row_that_fails_stops_the_campaign(tmp_path):
    """An arm aborts when its knobs did not take effect, and that invalidates
    every row after it too: the artifacts would be complete and wrongly
    labelled, which is indistinguishable from a measurement."""
    result = run_campaign(tmp_path, "run", env={"LADDER_EXIT": "3"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(result.rows) == 1, "the campaign continued past a failed row"
    assert "campaign stopped" in result.stderr


def test_the_listing_states_what_is_left_rather_than_only_what_exists(tmp_path):
    output = listing(tmp_path)
    assert "points planned" in output
    assert "left (~" in output


@pytest.mark.parametrize("bad", ["sweep", "--verbose"])
def test_an_unknown_verb_is_refused(tmp_path, bad):
    """A mistyped verb must not silently become the default: `list` is
    harmless, but a typo that ran the campaign instead would not be."""
    result = run_campaign(tmp_path, bad)
    assert result.returncode == 2, result.stdout


def test_no_argument_lists_rather_than_runs(tmp_path):
    result = run_campaign(tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.rows == [], "the default verb started a run"
    assert "points planned" in result.stdout
