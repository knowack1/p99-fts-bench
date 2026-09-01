"""Every command the Makefile generates must be accepted by its module's parser.

A Makefile target and the CLI it drives are edited by different hands at
different times, and a flag that was renamed on one side fails only when the
target is finally run — which, in a serialized campaign, is after the stack has
been brought up and an hour of measurement has been spent. `make -n` plus each
module's own `parse_args` catches it in a second instead.

This is why the campaign's fourth configuration and its remapped host ports were
found before the campaign rather than during it.
"""
import importlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent

TARGETS = (
    "os-load", "scylla-load",
    "c1-os", "c1-scylla-bootstrap", "c1-scylla-cdc",
    "c3-os", "c3-scylla-cdc",
    "c4-os", "c4-scylla",
    "c5-os", "c5-scylla", "c6-os", "c6-scylla",
    "c7-os", "c7-scylla",
    "calibrate-os", "calibrate-scylla",
    "c8-os", "c8-scylla-bootstrap", "c8-scylla-cdc",
)


def module_commands(target: str) -> list[tuple[str, list[str]]]:
    """The `python -m ftsbench.X` invocations one target would run.

    Line continuations are joined and shell operators split, because a C1 target
    puts the sampler and the measured work in one compound command.
    """
    result = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True)
    assert result.returncode == 0, f"make -n {target} failed:\n{result.stderr}"
    joined = result.stdout.replace("\\\n", " ")
    commands = []
    for line in re.split(r"[;&]+", joined.replace("\n", ";")):
        if "-m ftsbench." not in line:
            continue
        argv = shlex.split(line.lstrip("@").strip())
        marker = argv.index("-m")
        commands.append((argv[marker + 1], argv[marker + 2:]))
    return commands


def assert_parser_accepts(module_name: str, args: list[str]) -> None:
    module = importlib.import_module(module_name)
    saved = sys.argv
    sys.argv = [module_name] + args
    try:
        module.parse_args()
    except SystemExit as exit_signal:
        if exit_signal.code not in (0, None):
            pytest.fail(f"{module_name} rejected: {' '.join(args)}")
    finally:
        sys.argv = saved


@pytest.mark.parametrize("target", TARGETS)
def test_target_commands_are_accepted_by_their_parsers(target):
    commands = module_commands(target)
    assert commands, f"{target} runs no ftsbench module"
    for module_name, args in commands:
        assert_parser_accepts(module_name, args)


def test_manifest_probes_the_remapped_host_ports():
    """docker/.env moves the host bindings off 9042/6080 because a devcontainer
    holds them. A manifest that probed the defaults would record reachable:false
    for a healthy run, quietly emptying the campaign's provenance records."""
    from ftsbench import run_manifest
    assert run_manifest.default_scylla_port() == 19042
    assert run_manifest.default_vs_url() == "http://localhost:16080"


def test_manifest_accepts_every_campaign_configuration():
    """The four configurations in results/laptop-simplewiki-2026-08/README.md are
    argparse `choices`, so a missing one is a hard rejection at run time."""
    from ftsbench import run_manifest
    for config in ("opensearch", "opensearch-refresh30",
                   "scylla-bootstrap", "scylla-cdc"):
        assert config in run_manifest.CONFIGS


def target_script_argv(target: str, script: str) -> list[str]:
    result = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True)
    assert result.returncode == 0, f"make -n {target} failed:\n{result.stderr}"
    joined = result.stdout.replace("\\\n", " ")
    for line in re.split(r"[;&]+", joined.replace("\n", ";")):
        if script in line:
            argv = shlex.split(line.lstrip("@").strip())
            return argv[argv.index(script) + 1:]
    pytest.fail(f"{target} does not run {script}")


@pytest.mark.parametrize("target,engine",
                         [("co-check-os", "opensearch"),
                          ("co-check-scylla", "scylladb")])
def test_coordinated_omission_gate_is_invocable(target, engine):
    """The CO gate is a precondition for C5 and C7, so its own invocation must
    not be the thing that breaks. A closed-loop generator produces percentiles
    describing an engine that was never overloaded."""
    sys.path.insert(0, str(BENCH_DIR / "tools"))
    import co_check

    argv = target_script_argv(target, "tools/co_check.py")
    saved = sys.argv
    sys.argv = ["co_check.py"] + argv
    try:
        args = co_check.parse_args()
    finally:
        sys.argv = saved
    assert args.engine == engine
    assert args.rate > 0, "the gate must offer a rate above capacity"


OPENSEARCH_ARTIFACT_TARGETS = (
    ("c1-os", "C1_OS_SERIES"), ("c1-os", "C1_OS_MANIFEST"),
    ("c3-os", "C3_OS_LOG"), ("c3-os", "C3_PROBE_OS_OUT"), ("c4-os", "C4_OS_OUT"),
    ("c5-os", "C5_OS_LOG"), ("c6-os", "C6_OS_LOG"), ("c7-os", "C7_OS_OUT"), ("c8-os", "C8_OS_OUT"),
)

# C1, C3 and C8 already name the path in the target itself
# (c1-scylla-bootstrap / c1-scylla-cdc); these three targets are shared by both
# paths and so must be told which one is running.
SCYLLA_ARTIFACT_TARGETS = (
    ("c4-scylla", "C4_SCYLLA_OUT"), ("c5-scylla", "C5_SCYLLA_LOG"),
    ("c6-scylla", "C6_SCYLLA_LOG"),
    ("c7-scylla", "C7_SCYLLA_OUT"),
)


def expanded_variable(target: str, variable: str, override: str) -> str:
    result = subprocess.run(
        ["make", "-n", target, override,
         f"--eval=print-it:;@echo $({variable})", "print-it"],
        cwd=BENCH_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def assert_distinct_across_configs(target: str, variable: str, setting: str,
                                   configs: tuple[str, ...]) -> None:
    paths = {expanded_variable(target, variable, f"{setting}={config}")
             for config in configs}
    assert len(paths) == len(configs), \
        f"{variable} collides between configurations: {paths}"


@pytest.mark.parametrize("target,variable", OPENSEARCH_ARTIFACT_TARGETS)
def test_both_opensearch_configurations_write_distinct_artifacts(target, variable):
    """The two OpenSearch configurations differ only in refresh_interval, so
    with a shared artifact name the second silently overwrites the first and the
    chart draws one configuration twice under two labels."""
    assert_distinct_across_configs(target, variable, "OS_CONFIG",
                                   ("opensearch", "opensearch-refresh30"))


@pytest.mark.parametrize("target,variable", SCYLLA_ARTIFACT_TARGETS)
def test_both_scylla_paths_write_distinct_artifacts(target, variable):
    """The campaign runs every bootstrap repetition and then every CDC
    repetition. Sharing a name means the CDC reps overwrite the bootstrap reps
    with no error and no warning, and the bootstrap path's resource and query
    data is gone — while plot_c5 and plot_c7 document globs (c5-scylla-cdc-*)
    that would then match nothing at all."""
    assert_distinct_across_configs(target, variable, "SCYLLA_CONFIG",
                                   ("scylla-bootstrap", "scylla-cdc"))


def test_c1_report_counts_each_opensearch_repetition_once():
    """data/c1-opensearch-*.jsonl also matches every c1-opensearch-refresh30-*
    file, so the refresh=30s repetitions were summarized twice — once under each
    configuration."""
    glob = expanded_variable("c1-report", "C1_SERIES_GLOB", "REP=1")
    assert "c1-opensearch-[0-9]*.jsonl" in glob
    assert "c1-opensearch-*.jsonl" not in glob


def test_c1_target_fails_when_the_loader_fails():
    """`wait $MON` yields the sampler's status, not the loader's, so a loader
    that died mid-build left the target reporting success and the campaign gated
    on a truncated series rather than on the failure. This reads the recipe
    because provoking it for real needs a live stack."""
    result = subprocess.run(["make", "-n", "c1-os"], cwd=BENCH_DIR,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recipe = result.stdout
    assert "LOAD_RC=$?" in recipe, "the loader's status is never captured"
    assert recipe.index("LOAD_RC=$?") < recipe.index("wait $"), \
        "captured after the wait, which has already overwritten it"
    assert "exit $LOAD_RC" in recipe, "captured but never returned"


C3_TARGETS = ("c3-os", "c3-scylla-cdc")


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_runs_a_resource_probe_beside_the_load(target):
    """C3 was the only phase measured without one, which is why the 1.9x step in
    ScyllaDB's p50 between repetitions could not be diagnosed. A probe in a
    separate phase would not do: the contention being tested for only exists
    while the paced write load is running."""
    recipe = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True).stdout
    assert "-m ftsbench.resource_probe" in recipe
    assert recipe.index("-m ftsbench.resource_probe") < recipe.index("--latency-log"), \
        "the probe must start before the load, not after it"


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_returns_the_loader_status_not_the_probe_status(target):
    """`wait` overwrites $?, and the probe is killed rather than allowed to
    exit, so without capturing first a failed load would report success — the
    same defect already fixed once in c1-os."""
    recipe = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True).stdout
    assert "LOAD_RC=$?" in recipe, "the loader's status is never captured"
    assert recipe.index("LOAD_RC=$?") < recipe.index("wait $"), \
        "captured after the wait, which has already overwritten it"
    assert "exit $LOAD_RC" in recipe, "captured but never returned"


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_probe_is_signalled_rather_than_given_a_guessed_duration(target):
    """The C3 window is set by the loader. A --duration guess would either
    truncate the series or leave the probe running into the next phase."""
    recipe = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True).stdout
    assert "--duration 0" in recipe
    assert "kill -TERM $PROBE" in recipe


def test_c3_scylla_probe_watches_both_services():
    """The contention hypothesis is about ScyllaDB and the vector-store sharing
    the engine cpuset, so a probe watching one of them cannot test it."""
    recipe = subprocess.run(["make", "-n", "c3-scylla-cdc"], cwd=BENCH_DIR,
                            capture_output=True, text=True).stdout
    assert "fts-bench-scylla:scylladb" in recipe
    assert "fts-bench-vector-store:vector-store" in recipe


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_warns_when_the_probe_collected_nothing(target):
    """A dead probe leaves C3's own metric intact, so it must not abort the
    repetition — but silence is how this phase came to be unmeasurable once."""
    recipe = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True).stdout
    assert "WARNING: C3 resource probe wrote nothing" in recipe


def effective_flag(target: str, flag: str) -> str:
    """The value argparse would use: the last occurrence wins, and C3 overrides
    the batch size baked into OS_LOAD / SCYLLA_LOAD by appending its own."""
    recipe = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                            capture_output=True, text=True).stdout
    found = re.findall(rf"{re.escape(flag)}\s+(\S+)", recipe)
    assert found, f"{target} never passes {flag}"
    return found[-1]


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_offers_enough_operations_per_bucket_to_draw_a_tail(target):
    """The laptop C3 png was blank: one latency covers one batch, so operations
    per bucket is rate / batch * bucket_s, and at the global batch of 500 that
    was 20 against a floor of 100 for p99 and 1,000 for p999. Every bucket was
    refused. This is arithmetic, not hardware -- 73x the corpus buys more
    buckets, not deeper ones, so AWS would have reproduced the same empty chart.
    """
    from ftsbench.plot_c3 import DEFAULT_BUCKET_S
    from ftsbench.stats import min_samples_for

    rate = float(effective_flag(target, "--target-rate"))
    batch = float(effective_flag(target, "--batch-size"))
    per_bucket = rate / batch * DEFAULT_BUCKET_S
    assert per_bucket >= min_samples_for(99.9), (
        f"{target} offers {per_bucket:g} operations per {DEFAULT_BUCKET_S:g} s "
        f"bucket ({rate:g} docs/s / batch {batch:g}); p999 needs "
        f"{min_samples_for(99.9):,}")


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_stays_under_the_generator_ceiling(target):
    """Shrinking the batch to deepen the buckets raises operations per second,
    and C7 was invalidated by exactly that -- the HTTP client saturates near
    1,090 ops/s. A C3 that outruns its own generator measures the generator."""
    rate = float(effective_flag(target, "--target-rate"))
    batch = float(effective_flag(target, "--batch-size"))
    assert rate / batch <= 500, (
        f"{target} offers {rate / batch:g} operations/s, too close to the "
        "measured HTTP generator ceiling of ~1,090")
