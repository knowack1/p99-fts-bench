"""The ladder driver, without a stack: which points it runs and where they go.

The ladder no longer assembles the measured commands — tools/build_rate_point.sh
does, and tests/test_build_rate_point.py pins those against the Makefile. What
is under test here is the driver's own arithmetic: the rungs, the repetitions,
the batch levels, the directories and the set-aside, which is where an axis is
either right or silently wrong.

Two properties cannot be checked by reading it:

- with `BATCHES` unset the run has to be the run it was. Every measurement in
  results/ was taken by this script, and a driver that quietly moved its
  artifacts or changed a point would make the next pass non-comparable with the
  last for a reason nobody wrote down. `test_the_points_are_the_points_it_always_ran`
  compares against the committed script point for point.
- with `BATCHES` set, each level's artifacts have to land in `$OUT_DIR/b<batch>/`
  under UNCHANGED filenames. That is what lets `SERIES_RE` stay as it is and
  every summary over one directory be internally single-batch — the property
  `sweep_build_rate.py` asserts on the reading side.

The stack is stubbed: `make` records its argv (and, for the committed script,
writes the series its point gate reads), `docker` is inert, and the interpreter
is a stub that records every ftsbench invocation, writes the series when asked
to be the build monitor, and sleeps as the probes. The point script itself is
REAL, so what these tests drive is the ladder-to-point boundary rather than a
reimplementation of it.
"""
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from .test_makefile_commands import parse_module_commands

BENCH_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BENCH_DIR / "tools" / "sweep_build_rate.sh"
OPENSEARCH_ARM = "--opensearch-ram-nostore-refresh3"
SCYLLA_ARM = "--scylladb-cdc-buf376"
SWEEP_DOCS = 1000

MAKE_STUB = r"""#!/usr/bin/env bash
# The stack and the index DDL are all the ladder asks make for now. Recorded so
# the tests can assert that one engine comes up and both go down.
line=$(printf '%q ' "$@")
printf '%s\n' "$line" >> "$MAKE_LOG"
exit 0
"""

DOCKER_STUB = r"""#!/usr/bin/env bash
# `docker logs` and `docker exec -i cqlsh` are both called with input attached,
# so a stub that read stdin unconditionally would hang the ladder rather than
# fail it.
exit 0
"""

PYTHON_STUB = r"""#!/usr/bin/env bash
# Every ftsbench invocation is recorded as it would have been issued, so the
# tests read one log with the same parser test_makefile_commands uses. Two
# modules cannot be stubbed away: ftsbench.target resolves the arm, and the
# point-completeness gate is an inline script (`python - <series> <cap>`).
for arg in "$@"; do
  case "$arg" in
    ftsbench.target) exec "$REAL_PYTHON" "$@" ;;
  esac
done
case "$1" in
  -) exec "$REAL_PYTHON" "$@" ;;
esac
# ONE write: the probes are backgrounded, so two processes append here at the
# same time, and a line built from two printf calls can interleave with
# another's. That showed up as a point whose probe "did not exist".
line=$(printf '%q ' "$@")
printf '%s\n' "$line" >> "$PYTHON_LOG"
for arg in "$@"; do
  case "$arg" in
    ftsbench.resource_probe|ftsbench.generator_probe) exec sleep 600 ;;
  esac
done
# The monitor is what writes the series the gate reads.
if [[ " $* " == *" ftsbench.build_monitor "* ]]; then
  output=""
  previous=""
  for arg in "$@"; do
    [[ "$previous" == "--output" ]] && output="$arg"
    previous="$arg"
  done
  if [[ -n "$output" ]]; then
    mkdir -p "$(dirname "$output")"
    printf '%s\n' '{"record": "header", "engine": "opensearch"}' > "$output"
    printf '{"record": "sample", "i": 0, "docs_indexed": %s}\n' \
      "$STUB_DOCS" >> "$output"
  fi
fi
exit 0
"""


def stub_path(tmp_path: Path) -> Path:
    stubs = tmp_path / "bin"
    stubs.mkdir(parents=True)
    for name, body in (("make", MAKE_STUB), ("docker", DOCKER_STUB),
                       ("python-stub", PYTHON_STUB)):
        script = stubs / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    return stubs


def run_sweep(tmp_path: Path, script: Path, arm: str, *, out_dir: Path,
              reps: str = "1", ladder: str = "8 16",
              env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    stubs = stub_path(tmp_path)
    make_log = tmp_path / "make.log"
    make_log.touch()
    python_log = tmp_path / "python.log"
    python_log.touch()
    environment = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "MAKE_LOG": str(make_log),
        "PYTHON_LOG": str(python_log),
        "STUB_DOCS": str(SWEEP_DOCS),
        "REAL_PYTHON": str(BENCH_DIR / ".venv" / "bin" / "python3"),
        "PYTHON": str(stubs / "python-stub"),
        "OUT_DIR": str(out_dir),
        "LADDER": ladder,
        "SWEEP_DOCS": str(SWEEP_DOCS),
        "WARMUP": "0",
        **(env or {}),
    }
    environment.pop("BATCHES", None)
    if env and "BATCHES" in env:
        environment["BATCHES"] = env["BATCHES"]
    result = subprocess.run(["bash", str(script), arm, reps],
                            cwd=BENCH_DIR, capture_output=True, text=True,
                            env=environment, timeout=300)
    result.make_log = make_log.read_text(encoding="utf-8")
    result.python_log = python_log.read_text(encoding="utf-8")
    return result


def flag(argv: list[str], name: str) -> str | None:
    if name not in argv:
        return None
    return argv[argv.index(name) + 1]


def points(result: subprocess.CompletedProcess) -> list[list[str]]:
    """One manifest invocation per point, in the order the ladder ran them.

    The manifest is the natural per-point record: it is the one command that
    carries the configuration, the repetition, the label, the series it belongs
    to and the batch size at once.
    """
    return [argv for module, argv in parse_module_commands(result.python_log)
            if module == "ftsbench.run_manifest"]


def probes(result: subprocess.CompletedProcess, module: str) -> list[list[str]]:
    return [argv for name, argv in parse_module_commands(result.python_log)
            if name == f"ftsbench.{module}"]


def canonical(point: list[str], out_dir: Path) -> tuple[str, ...]:
    """A point as the campaign cares about it: which file, which repetition,
    which level, under which label. Compared across two ladder versions that
    reach it by different mechanisms, so it names the result and not the route."""
    return (
        (flag(point, "--series") or "").replace(str(out_dir), "$OUT_DIR"),
        flag(point, "--rep") or "",
        flag(point, "--batch-size") or "",
        flag(point, "--label") or "",
    )


def artifacts(out_dir: Path) -> list[str]:
    return sorted(str(path.relative_to(out_dir))
                  for path in out_dir.rglob("*") if path.is_file())


def test_the_script_still_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_legacy_mode_writes_the_artifacts_it_always_wrote(tmp_path):
    """The names every reader depends on, written down rather than derived.

    `ftsbench/sweep_build_rate.py`'s SERIES_RE and `tools/plot_batch_ceiling.py`
    parse these, and data/sweep-aws is full of them. With BATCHES unset there
    are no level subdirectories, because every reader of that tree expects the
    series at the top."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out)
    assert run.returncode == 0, run.stderr[-2000:]
    assert artifacts(out) == [
        "c1-opensearch-ramindex-c16-1.jsonl",
        "c1-opensearch-ramindex-c8-1.jsonl",
    ]


@pytest.mark.parametrize("arm,config,batch", [
    (OPENSEARCH_ARM, "opensearch-ramindex", "500"),
    (SCYLLA_ARM, "scylla-cdc-buf376", ""),
])
def test_the_points_are_the_points_it_always_ran(tmp_path, arm, config, batch):
    """Which points, in which order, under which label — stated, not compared.

    This used to diff against `git show HEAD:tools/sweep_build_rate.sh`, which
    was the right baseline while the ladder was being rewritten and worthless
    the moment the rewrite landed: HEAD became the new script and the
    comparison started passing against itself. What the archived measurements
    actually pin is this enumeration, so it is written down. The ScyllaDB arm
    records no batch size, because one operation is one prepared INSERT."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, arm, out_dir=out, reps="2")
    assert run.returncode == 0, run.stderr[-2000:]
    label = f"build-rate sweep, {config}, concurrency=%s batch={batch or 1}"
    assert [canonical(point, out) for point in points(run)] == [
        (f"$OUT_DIR/c1-{config}-c8-1.jsonl", "1", batch, label % 8),
        (f"$OUT_DIR/c1-{config}-c16-1.jsonl", "1", batch, label % 16),
        (f"$OUT_DIR/c1-{config}-c8-2.jsonl", "2", batch, label % 8),
        (f"$OUT_DIR/c1-{config}-c16-2.jsonl", "2", batch, label % 16),
    ]


def test_legacy_mode_offers_the_batch_size_make_would_have_chosen(tmp_path):
    """`OS_BATCH_SIZE ?= $(BATCH_SIZE)` is 500, so resolving the value outside
    make has to reproduce that and not introduce a second default."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out")
    assert run.returncode == 0, run.stderr[-2000:]
    for point in points(run):
        assert flag(point, "--batch-size") == "500"


def test_a_caller_that_exports_the_shared_batch_size_still_gets_it(tmp_path):
    """Seven scripts in tools/ pass BATCH_SIZE=500. The resolution here has to
    reproduce make's own choice rather than override it."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    env={"BATCH_SIZE": "300"})
    assert flag(points(run)[0], "--batch-size") == "300"


def test_every_level_roots_its_artifacts_in_its_own_directory(tmp_path):
    """b<batch>/ with UNCHANGED filenames inside: that is what keeps SERIES_RE
    as it is and every summary over one directory internally single-batch."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    ladder="8", env={"BATCHES": "16 512"})
    assert run.returncode == 0, run.stderr[-2000:]
    written = artifacts(out)
    assert "b16/c1-opensearch-ramindex-c8-1.jsonl" in written
    assert "b512/c1-opensearch-ramindex-c8-1.jsonl" in written
    assert not any(re.match(r"c1-.*\.jsonl$", name) for name in written), \
        "a level's series also landed loose in OUT_DIR, where it has no level"


def test_each_level_is_the_batch_size_the_point_recorded(tmp_path):
    """The directory is a label; the recorded batch size is what the run
    actually did. plot_batch_ceiling refuses a tree where those two disagree,
    so they must be one decision here."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    ladder="8", env={"BATCHES": "16 512"})
    for point in points(run):
        batch = flag(point, "--batch-size")
        series = flag(point, "--series")
        assert f"/b{batch}/" in series, f"{series} is not under b{batch}/"


def test_the_level_reaches_the_label_that_reaches_all_three_records(tmp_path):
    """The label is the cheap independent second record: it lands in the series
    header, the manifest and both probes' headers at once, so no single
    omission can hide what a point offered the engine."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    ladder="8", env={"BATCHES": "128"})
    label = flag(points(run)[0], "--label")
    assert "concurrency=8" in label
    assert "batch=128" in label


def test_the_levels_are_an_inner_loop_so_one_container_serves_them_all(tmp_path):
    """One invocation per level would pay the cold-JVM cost once per level and
    land it directly on the axis being measured."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    reps="2", ladder="8", env={"BATCHES": "16 512"})
    order = [(flag(point, "--rep"), flag(point, "--batch-size"))
             for point in points(run)]
    assert order == [("1", "16"), ("1", "512"), ("2", "16"), ("2", "512")], order


def test_a_multi_level_sweep_of_the_scylla_arm_is_refused(tmp_path):
    """There is no ScyllaDB batch axis: every row goes as its own prepared
    statement, so --batch-size is a loop window inside the client and sweeping
    it would measure ftsbench. A run that produced those points would look
    exactly like a measurement of the engine."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, SCYLLA_ARM, out_dir=out,
                    env={"BATCHES": "16 64 128"})
    assert run.returncode == 2, run.stdout + run.stderr
    assert "no ScyllaDB batch axis" in run.stderr
    assert not out.exists(), "the refusal came after the run had begun"
    assert points(run) == []


def test_the_scylla_arm_may_still_be_pinned_at_one_level(tmp_path):
    """A single level is a pin, not a sweep — and `BATCHES=1` is how the
    ScyllaDB arm's points get a directory alongside OpenSearch's."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, SCYLLA_ARM, out_dir=out, ladder="8",
                    env={"BATCHES": "1"})
    assert run.returncode != 2, run.stderr[-2000:]
    assert "no ScyllaDB batch axis" not in run.stderr


@pytest.mark.parametrize("levels", ["16,64", "0", "-8", "many"])
def test_a_batch_list_that_is_not_a_list_of_levels_is_refused(tmp_path, levels):
    """`BATCHES="16,64"` would otherwise become one flag and a directory named
    b16,64 — a plausible-looking tree measuring nothing nameable."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    env={"BATCHES": levels})
    assert run.returncode == 2, run.stdout + run.stderr
    assert "positive integers" in run.stderr
    assert not out.exists()


def test_a_set_aside_point_records_which_level_it_was(tmp_path):
    """failed-points.log is one file for the whole invocation, so without the
    level two set-aside points at the same concurrency and repetition in
    different level directories are indistinguishable."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    ladder="8", env={"BATCHES": "16 512", "STUB_DOCS": "1"})
    assert run.returncode == 0, run.stderr[-2000:]
    failures = (out / "failed-points.log").read_text(encoding="utf-8")
    assert "batch=16" in failures and "batch=512" in failures


def test_a_set_aside_point_takes_its_whole_artifact_set_out_of_the_way(tmp_path):
    """An incomplete point silently lowers a rung of the median, so nothing it
    wrote may stay where a summariser globs. The paths come from the point
    script itself, which is what keeps one naming rule in one file."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    ladder="8", env={"STUB_DOCS": "1"})
    assert run.returncode == 0, run.stderr[-2000:]
    written = artifacts(out)
    assert "c1-opensearch-ramindex-c8-1.jsonl.failed" in written
    assert "c1-opensearch-ramindex-c8-1.jsonl" not in written


def test_every_point_starts_a_generator_probe_carrying_its_own_label(tmp_path):
    """G1's missing half. --containers has only ever named the three SUT
    containers, so nothing recorded the box the loader runs on, and every
    "the client was the bottleneck" reading was inferred from the shape of a
    throughput curve. The label has to be the POINT's, because
    ftsbench.verify_generator refuses a series whose label does not name the
    concurrency it was handed -- judging one point with another's series is the
    defect that check exists for."""
    result = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM,
                       out_dir=tmp_path / "out", ladder="8 16")
    assert result.returncode == 0, result.stderr[-2000:]
    started = probes(result, "generator_probe")
    recorded = points(result)
    assert len(started) == len(recorded), \
        "one generator probe per point, got %d for %d points" % (len(started),
                                                                 len(recorded))
    for argv in started:
        assert flag(argv, "--match") == "ftsbench.opensearch_load", argv
        label = flag(argv, "--label")
        assert "concurrency=" in label and "batch=" in label, label
    assert sorted(flag(argv, "--label") for argv in started) \
        == sorted(flag(point, "--label") for point in recorded), \
        "a probe carries a label that is not its point's"


def test_the_generator_series_lands_beside_the_point_it_measures(tmp_path):
    """In the level's own subdirectory, so a b<batch>/ tree stays internally
    single-batch for the CPU verdict as well as for the rates."""
    result = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM,
                       out_dir=tmp_path / "out", ladder="8",
                       env={"BATCHES": "16 512"})
    assert result.returncode == 0, result.stderr[-2000:]
    for argv in probes(result, "generator_probe"):
        output = flag(argv, "--output")
        batch = flag(argv, "--label").split("batch=")[1].split()[0]
        assert "/b%s/gen-" % batch in output, (batch, output)


def test_the_ladder_keeps_the_stack_to_one_engine(tmp_path):
    """Both stacks are taken down whichever one came up: a ladder that left its
    stack running hands the next arm a neighbour competing for the box."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    ladder="8")
    assert run.returncode == 0, run.stderr[-2000:]
    invoked = [shlex.split(line) for line in run.make_log.splitlines()]
    assert ["os-up", "os-wait", "os-relax-watermarks"] in invoked
    assert ["os-down"] in invoked and ["scylla-down"] in invoked
