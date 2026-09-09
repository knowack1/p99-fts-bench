"""The point script and the Makefile's C1 targets must issue the same run.

Two definitions of the measured commands exist on purpose. Nine diagnostic
scripts drive `make c1-os` / `make c1-scylla-cdc` — campaign_laptop.sh among
them — so those targets stay; the build-rate campaign runs
`tools/build_rate_point.sh`, where the monitor and loader invocations are
written out instead of assembled from ~90 lines of `?=` layering and two
`define` blocks.

Two definitions can drift, and a drift here is not a broken build: it is a
campaign that measured something other than what its charts say. This test is
what forbids it. Both sides are read by one parser
(`test_makefile_commands.parse_module_commands`), so a difference in how the
argv was recovered cannot masquerade as agreement.

Exactly two differences are permitted, each asserted rather than tolerated:

- `run_manifest --command`, which records what was actually executed. Leaving
  it saying `make c1-os` would make the manifest lie, and results_tree.py
  publishes that field into the results tree.
- `run_manifest --config` on the three ScyllaDB knob arms. The Makefile
  hardcodes the literal `scylla-cdc` (so `scylla-cdc-buf376` writes a manifest
  claiming to be `scylla-cdc`, with arm identity surviving only in the
  filename); the point script passes the arm the registry resolved. The test
  asserts the new value IS the arm's config, not merely that it differs.

Deliberately NOT a difference: on the ScyllaDB side neither `run_manifest` nor
`build_monitor` receives `--batch-size`, so `batch_size` stays null there and
"one operation is one INSERT" reaches the artifact through the label instead.
Recording 1 would be a better artifact and a change in what is recorded; it is
not this commit's business.
"""
import os
import subprocess
from pathlib import Path

import pytest

from .test_makefile_commands import parse_module_commands

BENCH_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BENCH_DIR / "tools" / "build_rate_point.sh"

CAP = 1000000
OUT_DIR = "data/parity"
IDLE = 60
SETTLE = 120
MAX_SECONDS = 2400
CACHE_STATE = "warm-container-fresh-index"

# Every arm the build-rate campaign can run, plus the disk-store arm the
# earlier passes used. `scylla-cdc` is the pre-registry label whose artifacts
# the Makefile's hardcode happens to match; the three knob arms are where it
# does not.
OPENSEARCH_ARMS = (
    "--opensearch-ram-nostore-refresh3",
    "--opensearch-ram-nostore-refresh30",
    "--opensearch-disk-store-refresh3",
)
SCYLLA_ARMS = (
    "--scylladb-cdc",
    "--scylladb-cdc-buf15",
    "--scylladb-cdc-buf376",
    "--scylladb-cdc-buf376-commit30",
)
CONCURRENCIES = (4, 8, 64, 256)
BATCHES = (16, 512)
# The laptop pins the generator away from the engine cpuset; the fleet isolates
# it by being on another machine and fleet_env.sh sets GEN_CPUSET empty. A bare
# `taskset -c ""` fails, so both must be covered.
CPUSETS = ("12-19", "")


def arm_facts(arm: str) -> dict[str, str]:
    """config / engine / refresh for one arm, from the registry itself."""
    from ftsbench import target
    resolved = target.by_flag(arm)
    return {
        "config": resolved.config,
        "engine": resolved.engine,
        "refresh": dict(resolved.env).get("OS_REFRESH", "3s"),
    }


def label_for(config: str, concurrency: int, batch: int) -> str:
    return f"build-rate sweep, {config}, concurrency={concurrency} batch={batch}"


def artifact_paths(config: str, concurrency: int, rep: int) -> dict[str, str]:
    stem = f"{config}-c{concurrency}-{rep}"
    return {
        "series": f"{OUT_DIR}/c1-{stem}.jsonl",
        "manifest": f"{OUT_DIR}/manifest-{stem}.json",
    }


def point_commands(arm: str, concurrency: int, rep: int, batch: int,
                   cpuset: str) -> list[tuple[str, list[str]]]:
    env = dict(os.environ, GEN_CPUSET=cpuset)
    result = subprocess.run(
        [str(SCRIPT), "--arm", arm, "--concurrency", str(concurrency),
         "--rep", str(rep), "--batch", str(batch), "--cap", str(CAP),
         "--out-dir", OUT_DIR, "--idle-timeout", str(IDLE),
         "--settle-timeout", str(SETTLE), "--max-seconds", str(MAX_SECONDS),
         "--cache-state", CACHE_STATE, "--dry-run"],
        cwd=BENCH_DIR, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"dry run failed:\n{result.stdout}\n{result.stderr}"
    return parse_module_commands(result.stdout)


def makefile_commands(arm: str, concurrency: int, rep: int, batch: int,
                      cpuset: str) -> list[tuple[str, list[str]]]:
    """The same point through the Makefile, with the overrides the sweep passes."""
    facts = arm_facts(arm)
    paths = artifact_paths(facts["config"], concurrency, rep)
    label = label_for(facts["config"], concurrency, batch)
    common = [
        f"C1_MAX_SECONDS={MAX_SECONDS}", f"MAX_DOCS={CAP}",
        f"C1_UNTIL_DOCS={CAP}", f"CACHE_STATE={CACHE_STATE}",
        f"C1_IDLE_TIMEOUT={IDLE}", f"C1_SETTLE_TIMEOUT={SETTLE}",
        f"INGEST_CONCURRENCY={concurrency}", f"REP={rep}", f"LABEL={label}",
        f"GEN_CPUSET={cpuset}",
    ]
    if facts["engine"] == "opensearch":
        target_name = "c1-os"
        common += [
            f"OS_REFRESH={facts['refresh']}", f"OS_CONFIG={facts['config']}",
            f"OS_BATCH_SIZE={batch}",
            f"C1_OS_SERIES={paths['series']}",
            f"C1_OS_MANIFEST={paths['manifest']}",
        ]
    else:
        target_name = "c1-scylla-cdc"
        common += [
            f"C1_SCYLLA_CDC_SERIES={paths['series']}",
            f"C1_SCYLLA_CDC_MANIFEST={paths['manifest']}",
        ]
    return parse_module_commands(_make_dry_run(target_name, common))


def _make_dry_run(target_name: str, overrides: list[str]) -> str:
    result = subprocess.run(["make", "-n", target_name, *overrides],
                            cwd=BENCH_DIR, capture_output=True, text=True)
    assert result.returncode == 0, f"make -n {target_name} failed:\n{result.stderr}"
    return result.stdout


def flagset(argv: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Options and positionals, so flag ORDER may differ but nothing else may.

    Compared as sets of values because a script and a Makefile have no reason
    to emit the same flags in the same sequence, and pinning the sequence would
    fail on a difference that changes no run.
    """
    options: dict[str, list[str]] = {}
    positionals: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            positionals.append(token)
            index += 1
            continue
        takes_value = index + 1 < len(argv) and not argv[index + 1].startswith("--")
        value = argv[index + 1] if takes_value else ""
        options.setdefault(token, []).append(value)
        index += 2 if takes_value else 1
    return {name: sorted(values) for name, values in options.items()}, positionals


def by_module(commands: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for module, argv in commands:
        assert module not in seen, f"{module} invoked twice in one point"
        seen[module] = argv
    return seen


def strip_permitted(module: str, options: dict[str, list[str]],
                    config: str) -> dict[str, list[str]]:
    if module != "ftsbench.run_manifest":
        return options
    kept = {name: values for name, values in options.items()
            if name != "--command"}
    kept["--config"] = [config]
    return kept


@pytest.mark.parametrize("arm", OPENSEARCH_ARMS + SCYLLA_ARMS)
@pytest.mark.parametrize("concurrency", CONCURRENCIES)
@pytest.mark.parametrize("cpuset", CPUSETS)
def test_the_point_script_issues_the_run_the_makefile_issues(arm, concurrency,
                                                             cpuset):
    facts = arm_facts(arm)
    batch = 512 if facts["engine"] == "opensearch" else 1
    script = by_module(point_commands(arm, concurrency, 1, batch, cpuset))
    makefile = by_module(makefile_commands(arm, concurrency, 1, batch, cpuset))

    for module, expected_argv in makefile.items():
        assert module in script, f"the point script never runs {module}"
        want_options, want_positionals = flagset(expected_argv)
        got_options, got_positionals = flagset(script[module])
        assert got_positionals == want_positionals, module
        assert (strip_permitted(module, got_options, facts["config"])
                == strip_permitted(module, want_options, facts["config"])), module


@pytest.mark.parametrize("arm", OPENSEARCH_ARMS)
@pytest.mark.parametrize("batch", BATCHES)
def test_every_batch_level_reaches_all_three_records(arm, batch):
    """The axis is worthless if one writer disagrees about the level it ran at.

    S28 was retracted because a build-rate artifact could not be audited from
    the files it produced.
    """
    script = by_module(point_commands(arm, 8, 1, batch, "12-19"))
    for module in ("ftsbench.run_manifest", "ftsbench.build_monitor",
                   "ftsbench.opensearch_load"):
        options, _ = flagset(script[module])
        assert options["--batch-size"] == [str(batch)], module


@pytest.mark.parametrize("arm", SCYLLA_ARMS)
def test_the_scylla_arm_offers_no_batch_and_no_second_in_flight_bound(arm):
    """One operation is one prepared INSERT, so --concurrency is exactly the
    number of INSERTs in flight. A knob that could be silently wrong does not
    exist on this side rather than merely being pinned."""
    script = by_module(point_commands(arm, 8, 1, 1, "12-19"))
    options, _ = flagset(script["ftsbench.scylla_load"])
    for absent in ("--batch-size", "--rows-in-flight", "--unlogged-batch-rows"):
        assert absent not in options, f"scylla_load was offered {absent}"
    assert "--batch-size" not in flagset(script["ftsbench.build_monitor"])[0]


@pytest.mark.parametrize("arm", SCYLLA_ARMS)
def test_the_manifest_records_the_arm_that_actually_ran(arm):
    """The Makefile's c1-scylla-cdc hardcodes the literal `scylla-cdc`, so all
    three knob arms would write manifests claiming to be the same deployment —
    the failure ftsbench/target.py exists to prevent, and the one the campaign's
    own smoke gate ("manifests carry the right config") cannot currently pass."""
    config = arm_facts(arm)["config"]
    script = by_module(point_commands(arm, 8, 1, 1, "12-19"))
    options, _ = flagset(script["ftsbench.run_manifest"])
    assert options["--config"] == [config]


@pytest.mark.parametrize("arm", OPENSEARCH_ARMS + SCYLLA_ARMS)
def test_the_label_reaches_every_record_and_both_probes(arm):
    """One label, four writers. verify_generator refuses a generator series
    whose label carries no concurrency=, so a probe labelled with another
    point's label is a gate that judges the wrong run."""
    facts = arm_facts(arm)
    batch = 512 if facts["engine"] == "opensearch" else 1
    label = label_for(facts["config"], 8, batch)
    script = by_module(point_commands(arm, 8, 1, batch, "12-19"))
    for module in ("ftsbench.run_manifest", "ftsbench.build_monitor",
                   "ftsbench.resource_probe", "ftsbench.generator_probe"):
        options, _ = flagset(script[module])
        assert options["--label"] == [label], module


@pytest.mark.parametrize("arm", SCYLLA_ARMS)
def test_the_scylla_side_is_probed_as_two_containers(arm):
    """It IS two services: a probe watching only fts-bench-scylla would
    understate the side by exactly the size of the index."""
    script = by_module(point_commands(arm, 8, 1, 1, "12-19"))
    options, _ = flagset(script["ftsbench.resource_probe"])
    assert options["--containers"] == sorted(["fts-bench-scylla:scylladb",
                                              "fts-bench-vector-store:vector-store"])


@pytest.mark.parametrize("arm", OPENSEARCH_ARMS)
def test_the_opensearch_side_is_probed_too(arm):
    """S14 and verify_cpu_usage need both engines: a plateau without CPU
    saturation is not an engine ceiling, and that verdict cannot be reached for
    an engine nobody sampled."""
    script = by_module(point_commands(arm, 8, 1, 512, "12-19"))
    options, _ = flagset(script["ftsbench.resource_probe"])
    assert options["--containers"] == ["fts-bench-opensearch:opensearch"]


def test_the_generator_probe_watches_the_loader_this_arm_uses():
    for arm, module in (("--opensearch-ram-nostore-refresh3",
                         "ftsbench.opensearch_load"),
                        ("--scylladb-cdc-buf376", "ftsbench.scylla_load")):
        batch = 512 if "opensearch" in arm else 1
        script = by_module(point_commands(arm, 8, 1, batch, "12-19"))
        options, _ = flagset(script["ftsbench.generator_probe"])
        assert options["--match"] == [module]
