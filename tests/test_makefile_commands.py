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
    "c1-os", "c1-scylla-cdc",
    "c3-os", "c3-scylla-cdc",
    "c4-os", "c4-scylla",
    "c5-os", "c5-scylla", "c6-os", "c6-scylla",
    "c7-os", "c7-scylla",
    "calibrate-os", "calibrate-scylla",
    "c8-os", "c8-scylla-cdc",
)


def module_commands(target: str,
                    overrides: tuple[str, ...] = ()) -> list[tuple[str, list[str]]]:
    """The `python -m ftsbench.X` invocations one target would run.

    Line continuations are joined and shell operators split, because a C1 target
    puts the sampler and the measured work in one compound command.
    """
    result = subprocess.run(["make", "-n", target, *overrides], cwd=BENCH_DIR,
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


def parser_exit_code(module_name: str, args: list[str]) -> int | None:
    """The parser's verdict on one command rather than an assertion about it: a
    target may legitimately produce a command its own parser must refuse."""
    module = importlib.import_module(module_name)
    saved = sys.argv
    sys.argv = [module_name] + args
    try:
        module.parse_args()
        return None
    except SystemExit as exit_signal:
        return exit_signal.code
    finally:
        sys.argv = saved


def assert_parser_accepts(module_name: str, args: list[str]) -> None:
    if parser_exit_code(module_name, args) not in (0, None):
        pytest.fail(f"{module_name} rejected: {' '.join(args)}")


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

# C1, C3 and C8 already name the configuration in the target itself
# (c1-scylla-cdc); these targets do not, and so must be told which one is
# running.
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

# Documents one recorded latency covers. OpenSearch takes it from the recipe;
# ScyllaDB has no batch flag at all, so its operation is one prepared INSERT.
NO_BATCH_TARGETS = ("c3-scylla-cdc",)

# Measured client ceilings, ops/s: the HTTP client saturates near 1,090 and the
# CQL client near 4,368 (results/client-model-2026-09-08). C7 was invalidated by
# outrunning the first of those, so each arm is held to the same fraction of its
# own transport's ceiling rather than to one number borrowed from the other.
GENERATOR_BUDGET = {"c3-os": 500.0, "c3-scylla-cdc": 2000.0}


def docs_per_operation(target: str) -> float:
    """What one recorded latency covers on this arm."""
    if target in NO_BATCH_TARGETS:
        return 1.0
    return float(effective_flag(target, "--batch-size"))


def rate_per_operation(target: str) -> float:
    return (float(effective_flag(target, "--target-rate"))
            / docs_per_operation(target))


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
    per_bucket = rate / docs_per_operation(target) * DEFAULT_BUCKET_S
    assert per_bucket >= min_samples_for(99.9), (
        f"{target} offers {per_bucket:g} operations per {DEFAULT_BUCKET_S:g} s "
        f"bucket ({rate:g} docs/s / {docs_per_operation(target):g} docs per "
        f"operation); p999 needs {min_samples_for(99.9):,}")


@pytest.mark.parametrize("target", C3_TARGETS)
def test_c3_stays_under_the_generator_ceiling(target):
    """Shrinking the batch to deepen the buckets raises operations per second,
    and C7 was invalidated by exactly that -- the HTTP client saturates near
    1,090 ops/s. A C3 that outruns its own generator measures the generator."""
    offered = rate_per_operation(target)
    budget = GENERATOR_BUDGET[target]
    assert offered <= budget, (
        f"{target} offers {offered:g} operations/s against a budget of "
        f"{budget:g} — too close to the measured client ceiling for this "
        "engine's transport")


C1_WRITERS = ("ftsbench.run_manifest", "ftsbench.build_monitor",
              "ftsbench.opensearch_load", "ftsbench.scylla_load")


def module_flag(target: str, module: str, flag: str,
                overrides: tuple[str, ...] = ()) -> str:
    """The value one module of one target would receive, last occurrence wins.

    Per module rather than per recipe, because the redundancy is the point: the
    manifest, the series header and the loader each carry the write shape, and a
    recipe-wide search would let one of the three go missing unnoticed.
    """
    for name, argv in module_commands(target, overrides):
        if name != module:
            continue
        found = [argv[i + 1] for i, token in enumerate(argv) if token == flag]
        assert found, f"{target}: {module} never receives {flag}"
        return found[-1]
    pytest.fail(f"{target} does not run {module}")


def c1_writers(target: str) -> tuple[str, ...]:
    ran = {name for name, _ in module_commands(target)}
    return tuple(module for module in C1_WRITERS if module in ran)


def test_c1_gives_opensearch_the_batch_size_its_wire_actually_has():
    """On OpenSearch a batch is N documents in one _bulk — one request the
    engine sees — so every writer on that arm has to name the same one."""
    for module in c1_writers("c1-os"):
        assert module_flag("c1-os", module, "--batch-size") == "500", \
            f"c1-os: {module} disagrees with the arm's batch size"


RETIRED_SCYLLA_FLAGS = ("--batch-size", "--rows-in-flight",
                        "--unlogged-batch-rows")


@pytest.mark.parametrize("flag", RETIRED_SCYLLA_FLAGS)
def test_no_scylla_arm_passes_a_shape_flag_the_loader_no_longer_has(flag):
    """ScyllaDB sends one prepared INSERT per document: there is no wire batch
    to size and no second in-flight bound beside --concurrency. The loader
    rejects all three, so a recipe still passing one would fail the run rather
    than reshape it."""
    for target in ("c1-scylla-cdc", "c3-scylla-cdc", "scylla-load"):
        for module, argv in module_commands(target):
            assert flag not in argv, \
                f"{target}: {module} was given the retired {flag}"


@pytest.mark.parametrize("target", ["c1-os", "c1-scylla-cdc"])
def test_c1_writes_both_records_that_describe_the_run(target):
    """results/aws-enwiki-2026-09/S28-RETRACTION.md is the failure already on
    record: a header that recorded no concurrency left a defect unauditable from
    the files the run produced. Both records must be written on both arms."""
    writers = c1_writers(target)
    assert "ftsbench.run_manifest" in writers, "the manifest is not written"
    assert "ftsbench.build_monitor" in writers, "the series header is not written"


def test_c1_opensearch_records_one_batch_size_across_its_artifacts():
    """The redundancy is the point: the manifest, the series header and the
    loader each carry the shape, so no single omission can hide what was
    offered — and a disagreement between them catches a sweep that passed one
    batch size to make and another to the loader."""
    recorded = {module_flag("c1-os", module, "--batch-size")
                for module in c1_writers("c1-os")}
    assert len(recorded) == 1, \
        f"c1-os records {sorted(recorded)} — the artifacts disagree"


def test_no_arm_records_the_retired_rows_in_flight_field():
    """`rows_in_flight` was a ScyllaDB-only bound that duplicated
    --concurrency, and the recorders no longer accept it. A number appearing
    there again would be a knob on an axis that has none."""
    for target in ("c1-os", "c1-scylla-cdc"):
        for module, argv in module_commands(target):
            assert "--rows-in-flight" not in argv, \
                f"{target}: {module} was given a retired flag"


@pytest.mark.parametrize("target,setting", [("c1-os", "OS_BATCH_SIZE")])
def test_the_sweep_is_the_authority_on_the_batch_size_it_records(target, setting):
    """tools/sweep_build_rate.sh resolves the level itself and passes it to
    make, because the same number has to reach the label, the header and the
    manifest at once. A command-line OS_BATCH_SIZE must therefore beat the
    historical BATCH_SIZE=500 that every legacy tools/ script still passes."""
    overrides = ("BATCH_SIZE=500", f"{setting}=16")
    for module in c1_writers(target):
        assert module_flag(target, module, "--batch-size", overrides) == "16", \
            f"{target}: {module} ignored a swept {setting}"


def test_a_legacy_caller_still_gets_the_historical_opensearch_batch():
    """Seven scripts in tools/ drive `make c1-os BATCH_SIZE=500`. Their
    OpenSearch points must keep landing at 500, or every one of them measures
    something other than what it measured before."""
    assert module_flag("c1-os", "ftsbench.opensearch_load", "--batch-size",
                       ("BATCH_SIZE=500",)) == "500"


def test_c3_records_what_one_latency_covers_on_each_arm():
    """C3 compares a per-operation latency, and the two arms no longer cover the
    same number of documents: OpenSearch records one C3_BATCH-document _bulk,
    ScyllaDB records one INSERT. That is a property of the wire — there is no
    CQL batch to match — so the chart has to say so rather than the harness
    pretending the quantities are equal."""
    assert docs_per_operation("c3-scylla-cdc") == 1
    assert docs_per_operation("c3-os") > 1


def test_c3_overrides_the_opensearch_macros_baked_in_batch_size():
    """OS_LOAD bakes in OS_BATCH_SIZE for the throughput path; C3 needs its own,
    smaller batch to get enough operations per bucket, and gets it by appending
    a second --batch-size that argparse's last-wins resolves."""
    loader = next(argv for name, argv in module_commands("c3-os")
                  if name.endswith("_load"))
    assert loader.count("--batch-size") == 2, \
        "c3-os no longer overrides the macro's baked-in batch size"


def test_the_c3_scylla_arm_carries_no_batch_flag_to_override():
    """Nothing to append: the loader has no such flag, and a C3 recipe that
    started passing one would be rejected by the parser rather than silently
    reshaping the arm."""
    loader = next(argv for name, argv in module_commands("c3-scylla-cdc")
                  if name.endswith("_load"))
    assert "--batch-size" not in loader


RETIRED_KNOBS = ("SCYLLA_BATCH_SIZE=500", "SCYLLA_ROWS_IN_FLIGHT=64",
                 "SCYLLA_UNLOGGED_BATCH_ROWS=30")


@pytest.mark.parametrize("override", RETIRED_KNOBS)
def test_a_caller_setting_a_removed_knob_is_refused_not_ignored(override):
    """Six scripts in tools/ drive the UNLOGGED BATCH mode, and others pin a
    ScyllaDB batch size. That mode is gone: one operation is one prepared
    INSERT. Ignoring the setting would run the per-row path under a label and a
    manifest saying 30 rows per batch — the S28 failure, a complete and
    plausible artifact describing a run nobody performed. make refuses."""
    refused = subprocess.run(["make", "-n", "c1-scylla-cdc", override],
                             cwd=BENCH_DIR, capture_output=True, text=True)
    assert refused.returncode != 0, f"{override} was accepted and ignored"
    assert override.split("=")[0] in refused.stderr


def test_the_campaign_targets_still_run_without_those_knobs():
    """The refusal must be about an explicit setting and nothing else."""
    for target in ("c1-scylla-cdc", "c3-scylla-cdc", "scylla-load"):
        done = subprocess.run(["make", "-n", target], cwd=BENCH_DIR,
                              capture_output=True, text=True)
        assert done.returncode == 0, f"{target}: {done.stderr}"


def test_the_campaign_scylla_load_command_is_still_accepted():
    """The refusal above must be about the combination and nothing else: the
    campaign's own per-row path (--unlogged-batch-rows 0 at --batch-size 1) has
    no group to fill and has to keep running."""
    assert_parser_accepts(*next((name, argv) for name, argv
                                in module_commands("scylla-load")
                                if name == "ftsbench.scylla_load"))
