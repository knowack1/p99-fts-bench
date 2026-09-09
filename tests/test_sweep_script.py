"""The sweep driver, without a stack: what it asks make for and where it puts it.

`BATCHES` adds an axis to a script every arm of the campaign already runs, and
two of its properties cannot be checked by reading it:

- with `BATCHES` unset the run has to be the run it was. Every measurement in
  results/ was taken by this script, and a driver that quietly moved its
  artifacts or changed a make variable would make the next pass
  non-comparable with the last for a reason nobody wrote down.
- with `BATCHES` set, each level's artifacts have to land in
  `$OUT_DIR/b<batch>/` under UNCHANGED filenames. That is what lets
  `SERIES_RE` stay as it is and every summary over one directory be
  internally single-batch — the property `sweep_build_rate.py` asserts on the
  reading side.

The stack is stubbed: `make` records its argv and writes the series the point
gate reads, and the resource probe sleeps. What is under test is the driver's
own arithmetic, which is where the axis is either right or silently wrong.
"""
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BENCH_DIR / "tools" / "sweep_build_rate.sh"
OPENSEARCH_ARM = "--opensearch-ram-nostore-refresh3"
SCYLLA_ARM = "--scylladb-cdc-buf376"
SWEEP_DOCS = 1000

MAKE_STUB = r"""#!/usr/bin/env bash
# Records every invocation, and writes the series the point gate reads so that
# a stubbed point completes rather than being set aside.
printf '%q ' "$@" >> "$MAKE_LOG"
printf '\n' >> "$MAKE_LOG"
series=""
for arg in "$@"; do
  case "$arg" in
    C1_OS_SERIES=*|C1_SCYLLA_CDC_SERIES=*) series="${arg#*=}" ;;
  esac
done
if [[ -n "$series" ]]; then
  mkdir -p "$(dirname "$series")"
  printf '%s\n' '{"record": "header", "engine": "opensearch"}' > "$series"
  printf '{"record": "sample", "i": 0, "docs_indexed": %s}\n' \
    "$STUB_DOCS" >> "$series"
fi
exit 0
"""

DOCKER_STUB = r"""#!/usr/bin/env bash
# `docker logs` is called with the script's stdin attached, so a stub that read
# stdin unconditionally would hang the sweep rather than fail it.
exit 0
"""

PYTHON_STUB = r"""#!/usr/bin/env bash
# The probes are the only long-running python the sweep starts; everything else
# (ftsbench.target, the point gate) has to be the real interpreter. Both probes
# are logged rather than run: what the tests assert is that each point starts
# one and hands it that point's own label, which is the invariant
# ftsbench.verify_generator depends on.
for arg in "$@"; do
  case "$arg" in
    ftsbench.resource_probe) exec sleep 600 ;;
    ftsbench.generator_probe)
      # %q per argument: the point label carries spaces, and joining with "$*"
      # would split it into tokens on the way back out.
      { printf 'generator_probe'; printf ' %q' "$@"; printf '\n'; } >> "$PROBE_LOG"
      exec sleep 600
      ;;
  esac
done
exec "$REAL_PYTHON" "$@"
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
    probe_log = tmp_path / "probe.log"
    probe_log.touch()
    environment = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "MAKE_LOG": str(make_log),
        "PROBE_LOG": str(probe_log),
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
    result.make_log = make_log.read_text(encoding="utf-8") if make_log.exists() else ""
    result.probe_log = probe_log.read_text(encoding="utf-8") if probe_log.exists() else ""
    return result


def c1_invocations(make_log: str) -> list[list[str]]:
    return [shlex.split(line) for line in make_log.splitlines()
            if line.startswith("c1-os ") or line.startswith("c1-scylla-cdc ")]


def variable(argv: list[str], name: str) -> str | None:
    for token in argv:
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    return None


def label_of(argv: list[str]) -> str:
    return variable(argv, "LABEL") or ""


def comparable(argv: list[str], out_dir: Path) -> set[str]:
    """Make variables with the run's own OUT_DIR folded out, and the label
    dropped: the two runs write to different directories, and the contract
    appends the write shape to the label, so both are compared separately."""
    return {token.replace(str(out_dir), "$OUT_DIR") for token in argv
            if not token.startswith("LABEL=")}


def artifacts(out_dir: Path) -> list[str]:
    return sorted(str(path.relative_to(out_dir))
                  for path in out_dir.rglob("*") if path.is_file())


def head_tree(tmp_path: Path) -> Path:
    """The committed script, runnable: it does `cd "$(dirname "$0")/.."`, so it
    needs a tree beside it. Everything but the script itself is a link to the
    real one, so the two runs differ in exactly one file."""
    committed = subprocess.run(["git", "show", "HEAD:tools/sweep_build_rate.sh"],
                               cwd=BENCH_DIR, capture_output=True, text=True)
    assert committed.returncode == 0, committed.stderr
    root = tmp_path / "head-tree"
    (root / "tools").mkdir(parents=True)
    for entry in BENCH_DIR.iterdir():
        if entry.name != "tools":
            (root / entry.name).symlink_to(entry)
    for entry in (BENCH_DIR / "tools").iterdir():
        if entry.name != "sweep_build_rate.sh":
            (root / "tools" / entry.name).symlink_to(entry)
    script = root / "tools" / "sweep_build_rate.sh"
    script.write_text(committed.stdout, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_the_script_still_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_legacy_mode_writes_the_artifacts_it_always_wrote(tmp_path):
    """Path-for-path against the committed script under the same stubs. With
    BATCHES unset there are no level subdirectories, because every reader of
    data/sweep-aws expects the series at the top."""
    now = run_sweep(tmp_path / "now", SCRIPT, OPENSEARCH_ARM,
                    out_dir=tmp_path / "out-now")
    before = run_sweep(tmp_path / "before", head_tree(tmp_path / "before"),
                       OPENSEARCH_ARM, out_dir=tmp_path / "out-before")
    assert now.returncode == 0, now.stderr[-2000:]
    assert before.returncode == 0, before.stderr[-2000:]
    assert artifacts(tmp_path / "out-now") == artifacts(tmp_path / "out-before")
    assert artifacts(tmp_path / "out-now"), "the run produced nothing to compare"
    assert not any("/" in name for name in artifacts(tmp_path / "out-now"))


def test_legacy_mode_changes_only_the_variables_the_contract_adds(tmp_path):
    """The one difference a legacy run is allowed: the batch value the Makefile
    would have chosen anyway, resolved here so the same number can also reach
    the point label. Anything else would make this pass non-comparable with the
    measured ones."""
    now = run_sweep(tmp_path / "now", SCRIPT, OPENSEARCH_ARM,
                    out_dir=tmp_path / "out-now")
    before = run_sweep(tmp_path / "before", head_tree(tmp_path / "before"),
                       OPENSEARCH_ARM, out_dir=tmp_path / "out-before")
    after_all = c1_invocations(now.make_log)
    prior_all = c1_invocations(before.make_log)
    assert after_all, "the run invoked no C1 target"
    assert len(after_all) == len(prior_all), \
        "legacy mode runs a different number of points than it used to"
    pairs = list(zip(after_all, prior_all))
    for new, old in pairs:
        after = comparable(new, tmp_path / "out-now")
        prior = comparable(old, tmp_path / "out-before")
        assert after - prior == {"OS_BATCH_SIZE=500"}, \
            f"legacy mode changed more than the batch value: {after - prior}"
        assert not prior - after, \
            f"a make variable the committed script passed has gone: {prior - after}"
        assert label_of(new) == f"{label_of(old)} batch=500", \
            "the label gained more than the write shape"


def test_legacy_mode_asks_make_for_the_batch_size_make_would_have_chosen(tmp_path):
    """`OS_BATCH_SIZE ?= $(BATCH_SIZE)` is 500, so resolving the value in the
    script has to reproduce that and not introduce a second default."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out")
    assert run.returncode == 0, run.stderr[-2000:]
    for argv in c1_invocations(run.make_log):
        assert variable(argv, "OS_BATCH_SIZE") == "500"


def test_a_caller_that_exports_the_shared_batch_size_still_gets_it(tmp_path):
    """Seven scripts in tools/ pass BATCH_SIZE=500. The resolution here has to
    reproduce make's own choice rather than override it."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    env={"BATCH_SIZE": "300"})
    assert variable(c1_invocations(run.make_log)[0], "OS_BATCH_SIZE") == "300"


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


def test_each_level_is_the_batch_size_it_asks_make_for(tmp_path):
    """The directory is a label; the make variable is what the run actually
    did. plot_batch_ceiling refuses a tree where those two disagree, so they
    must be one decision here."""
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    ladder="8", env={"BATCHES": "16 512"})
    for argv in c1_invocations(run.make_log):
        batch = variable(argv, "OS_BATCH_SIZE")
        series = variable(argv, "C1_OS_SERIES")
        assert f"/b{batch}/" in series, f"{series} is not under b{batch}/"


def test_the_level_reaches_the_label_that_reaches_all_three_records(tmp_path):
    """The label is the cheap independent second record: it lands in the series
    header, the manifest and the CPU probe's header at once, so no single
    omission can hide what a point offered the engine."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    ladder="8", env={"BATCHES": "128"})
    label = variable(c1_invocations(run.make_log)[0], "LABEL")
    assert "concurrency=8" in label
    assert "batch=128" in label


def test_the_levels_are_an_inner_loop_so_one_container_serves_them_all(tmp_path):
    """One invocation per level would pay the cold-JVM cost once per level and
    land it directly on the axis being measured."""
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=tmp_path / "out",
                    reps="2", ladder="8", env={"BATCHES": "16 512"})
    order = [(variable(argv, "REP"), variable(argv, "OS_BATCH_SIZE"))
             for argv in c1_invocations(run.make_log)]
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
    assert c1_invocations(run.make_log) == []


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
    """`BATCHES="16,64"` would otherwise become one make variable and a
    directory named b16,64 — a plausible-looking tree measuring nothing
    nameable."""
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
    stubs_env = {"BATCHES": "16 512", "STUB_DOCS": "1"}
    out = tmp_path / "out"
    run = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM, out_dir=out,
                    ladder="8", env=stubs_env)
    assert run.returncode == 0, run.stderr[-2000:]
    failures = (out / "failed-points.log").read_text(encoding="utf-8")
    assert "batch=16" in failures and "batch=512" in failures


def generator_probes(probe_log: str) -> list[list[str]]:
    return [shlex.split(line) for line in probe_log.splitlines()
            if line.startswith("generator_probe ")]


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
    probes = generator_probes(result.probe_log)
    points = c1_invocations(result.make_log)
    assert len(probes) == len(points), \
        "one generator probe per point, got %d for %d points" % (len(probes), len(points))
    for argv in probes:
        assert "--match" in argv, argv
        assert argv[argv.index("--match") + 1] == "ftsbench.opensearch_load", argv
        label = argv[argv.index("--label") + 1]
        assert "concurrency=" in label and "batch=" in label, label
    labels = [argv[argv.index("--label") + 1] for argv in probes]
    assert sorted(labels) == sorted(label_of(argv) for argv in points), \
        "a probe carries a label that is not its point's"


def test_the_generator_series_lands_beside_the_point_it_measures(tmp_path):
    """In the level's own subdirectory, so a b<batch>/ tree stays internally
    single-batch for the CPU verdict as well as for the rates."""
    result = run_sweep(tmp_path, SCRIPT, OPENSEARCH_ARM,
                       out_dir=tmp_path / "out", ladder="8",
                       env={"BATCHES": "16 512"})
    assert result.returncode == 0, result.stderr[-2000:]
    for argv in generator_probes(result.probe_log):
        out = argv[argv.index("--output") + 1]
        batch = argv[argv.index("--label") + 1].split("batch=")[1].split()[0]
        assert "/b%s/gen-" % batch in out, (batch, out)
