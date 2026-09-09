"""The campaign script: nine literal runs, and what each one offers the ladder.

The file is the list — no table, no parsing, no computed rows — so what is
worth testing is not its arithmetic but that the nine lines say what
BUILD-RATE-MATRIX-PLAN.md decided: the five knob arms at N=3 over the seven
rungs, the batch axis on the two OpenSearch arms at their measured c_sat with a
pin probe at twice that, the 1M cap and the warm-up on every run, and no batch
level anywhere near a ScyllaDB arm.

The ladder is stubbed — what is under test is the campaign, not the loop it
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


def nproc() -> int:
    """The campaign's own denominator, asked for the same way the script asks:
    `nproc` honours the affinity mask and os.cpu_count() does not."""
    return int(subprocess.run(["nproc"], capture_output=True, text=True,
                              check=True).stdout)


KNOB_ARMS = (
    "--scylladb-cdc-buf15",
    "--scylladb-cdc-buf376",
    "--scylladb-cdc-buf376-commit30",
    "--opensearch-ram-nostore-refresh3",
    "--opensearch-ram-nostore-refresh30",
)

LADDER_STUB = r"""#!/usr/bin/env bash
# Records the arm, the reps and every knob the campaign line set for this run.
printf 'run arm=%q reps=%q OUT_DIR=%q LADDER=%q BATCHES=%q OS_BATCH_SIZE=%q SWEEP_DOCS=%q WARMUP=%q WORKERS=%q DRY_RUN=%q\n' \
  "$1" "${2:-}" "${OUT_DIR:-}" "${LADDER:-}" "${BATCHES:-}" "${OS_BATCH_SIZE:-}" \
  "${SWEEP_DOCS:-}" "${WARMUP:-}" "${WORKERS:-}" "${DRY_RUN:-}" >> "$LADDER_LOG"
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


def run_campaign(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    tree = stub_tree(tmp_path)
    ladder_log = tmp_path / "ladder.log"
    ladder_log.touch()
    environment = {
        **os.environ,
        "LADDER_LOG": str(ladder_log),
        "ROOT": str(tmp_path / "artifacts"),
        **(env or {}),
    }
    result = subprocess.run(
        ["bash", str(tree / "tools" / "build_rate_campaign.sh"), *args],
        cwd=tree, capture_output=True, text=True, env=environment, timeout=120)
    result.runs = [dict(token.split("=", 1) for token in shlex.split(line)[1:])
                   for line in ladder_log.read_text(encoding="utf-8").splitlines()
                   if line.startswith("run ")]
    return result


def runs(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    result = run_campaign(tmp_path, *(args or ("run",)), env=env)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.runs, "the campaign ran nothing"
    return result.runs


def test_the_script_still_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_every_run_names_an_arm_the_registry_knows(tmp_path):
    """A flag the registry does not carry fails when the line runs, which on a
    serialized campaign is after the stack is up and an hour is spent."""
    known = {arm.flag for arm in target.TARGETS}
    named = {run["arm"] for run in runs(tmp_path)}
    assert named <= known, f"not in ftsbench/target.py: {named - known}"


def test_the_knob_matrix_runs_five_arms_once_each(tmp_path):
    """BUILD-RATE-MATRIX-PLAN.md's run table: R1 prices the writer buffer, R2
    adds parity, R3 adds the slow cadence, R4/R5 are the OpenSearch pair at the
    matching refresh cadences. A missing arm is a knob delta nobody measured."""
    laddered = [run for run in runs(tmp_path) if run["OUT_DIR"].endswith("/knobs")]
    assert [run["arm"] for run in laddered] == list(KNOB_ARMS)
    for run in laddered:
        assert run["LADDER"] == "4 8 16 32 64 96 128", run


def test_r1_runs_before_r2_so_the_writer_buffer_is_priced_first(tmp_path):
    """If R2 does not beat R1 by roughly the measured 1.42x, the generator is
    binding and the rest of the matrix is measuring the client. That is only
    knowable in time to act on it if R1 goes first."""
    order = [run["arm"] for run in runs(tmp_path)]
    assert order.index("--scylladb-cdc-buf15") < order.index("--scylladb-cdc-buf376")


def test_no_scylla_run_carries_a_batch_level(tmp_path):
    """On OpenSearch a batch is a wire batch the engine sees. On ScyllaDB every
    row is its own prepared statement, so a level would be a dispatch window
    inside the client and the curve would measure ftsbench. The ladder refuses
    such a run outright; the campaign must not ask for one."""
    for run in runs(tmp_path):
        if "scylladb" in run["arm"]:
            assert run["BATCHES"] == "", run


def test_the_batch_axis_pins_each_arm_at_its_c_sat_and_probes_twice_that(tmp_path):
    """Offered document pressure is c x batch, so the c_sat measured at batch
    512 can sit below the c_sat at batch 16 — pinning one concurrency would
    under-report the small levels by exactly the amount that confirms "a bigger
    batch is faster". Hence the probe at 2 x c_sat, at the same N as the
    measurement so its verdict carries a spread of its own."""
    axis = [run for run in runs(tmp_path) if run["OUT_DIR"].endswith("/batch")]
    assert len(axis) == 4, axis
    for run in axis:
        assert run["BATCHES"] == "16 64 128 256 512", run
        assert "opensearch" in run["arm"], run
    assert [run["LADDER"] for run in axis] == ["8", "16", "8", "16"]


def test_every_run_uses_the_same_repetition_count(tmp_path):
    """No line is quietly less certain than its neighbours: a curve drawn from
    a mixture of N=3 and N=1 points has a spread on some markers and not on
    others, and nothing on the chart says which."""
    counts = {run["reps"] for run in runs(tmp_path / "default")}
    assert counts == {"3"}, counts
    overridden = {run["reps"] for run in
                  runs(tmp_path / "override", env={"REPS": "5"})}
    assert overridden == {"5"}, overridden


def test_the_opensearch_knob_runs_offer_the_locked_batch_size(tmp_path):
    """"Decisions locked" says OpenSearch 512. With OS_BATCH_SIZE unset the
    ladder falls back to the Makefile's historical BATCH_SIZE=500, which is not
    the decision and is not comparable with the batch axis's top level. The
    ScyllaDB arms take no batch flag: one operation is one prepared INSERT."""
    for run in runs(tmp_path):
        if not run["OUT_DIR"].endswith("/knobs"):
            continue
        if "opensearch" in run["arm"]:
            assert run["OS_BATCH_SIZE"] == "512", run
        else:
            assert run["OS_BATCH_SIZE"] == "", run


def test_the_two_shapes_never_share_a_directory(tmp_path):
    """A concurrency ladder and a batch axis are different trees: the
    summariser reads a directory as one shape, and plot_batch_ceiling refuses a
    tree that holds a point from another level."""
    groups = {run["OUT_DIR"] for run in runs(tmp_path)}
    assert sorted(path.rsplit("/", 1)[1] for path in groups) == ["batch", "knobs"]


def test_every_run_uses_the_locked_cap_and_keeps_its_warm_up(tmp_path):
    """1,000,000 documents per point and one discarded warm-up per invocation,
    both from "Decisions locked". The warm-up is what makes the median of 3
    safe: the median of 3 is the middle value, which one cold repetition can
    move."""
    for run in runs(tmp_path):
        assert run["SWEEP_DOCS"] == "1000000", run
        assert run["WARMUP"] == "1", run


def test_a_batch_level_left_in_the_environment_does_not_reach_a_run(tmp_path):
    """An inherited BATCHES would turn a concurrency ladder into a batch sweep:
    a complete, plausible, mislabelled curve."""
    laddered = [run for run in runs(tmp_path, env={"BATCHES": "64"})
                if run["OUT_DIR"].endswith("/knobs")]
    for run in laddered:
        assert run["BATCHES"] == "", run


def test_every_run_gets_half_the_box_as_loader_processes(tmp_path):
    """One process delivered 8,003 docs/s on the ScyllaDB client, 0.66x of a
    ~12.2k engine ceiling, which G7's 2x rule refuses; N=4 is 2.34x. Half the
    box leaves the other half for the parent, the mp.Manager, the monitor and
    the open-loop generator. Every arm gets the same count — R4/R5 included, so
    R2<->R4 and R3<->R5 do not compare across two client shapes."""
    expected = str(max(nproc() // 2, 1))
    for run in runs(tmp_path):
        assert run["WORKERS"] == expected, run


def test_the_worker_count_can_be_pinned_for_a_differently_shaped_box(tmp_path):
    """Half of nproc is a budget for the as-built 8-vCPU fleet, not a law: a box
    whose loader ceiling sits elsewhere is pinned rather than divided."""
    for run in runs(tmp_path, env={"WORKERS": "2"}):
        assert run["WORKERS"] == "2", run


def test_dry_run_reaches_every_line_without_measuring_anything(tmp_path):
    """The campaign's own preview: the same nine invocations, each told to print
    what it would do rather than do it."""
    previewed = runs(tmp_path, "dry-run")
    assert len(previewed) == 9, previewed
    for run in previewed:
        assert run["DRY_RUN"] == "1", run


def test_a_run_that_fails_stops_the_campaign(tmp_path):
    """An arm aborts when its knobs did not take effect, and that invalidates
    every run after it too: the artifacts would be complete and wrongly
    labelled, which is indistinguishable from a measurement."""
    result = run_campaign(tmp_path, "run", env={"LADDER_EXIT": "3"})
    assert result.returncode != 0, result.stdout
    assert len(result.runs) == 1, "the campaign continued past a failed run"


@pytest.mark.parametrize("verb", ["", "list", "--help", "sweep"])
def test_nothing_but_run_or_dry_run_starts_the_campaign(tmp_path, verb):
    """There is no default verb: the cheapest way to spend a fleet day by
    accident is a script that measures when it is asked for a listing."""
    result = run_campaign(tmp_path, *( [verb] if verb else [] ))
    assert result.returncode == 2, result.stdout
    assert result.runs == []


def test_the_finished_campaign_names_what_turns_points_into_numbers(tmp_path):
    result = run_campaign(tmp_path, "run")
    assert result.returncode == 0, result.stderr[-2000:]
    for expected in ("ftsbench.sweep_build_rate", "ftsbench.verify_cpu_usage",
                     "ftsbench.verify_generator", "plot_batch_ceiling.py"):
        assert expected in result.stdout, expected
