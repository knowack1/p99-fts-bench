"""The campaign driver's own gates and settings, without running a campaign.

The first smoke run of the full C1-C8 campaign aborted all four repetitions on
two gates that were wrong rather than on measurements that were bad: one asked
for twenty samples from a build that could only produce five, and one matched
the vector-store's own startup banner. Both cost a full run to discover. These
tests exercise the same code paths in milliseconds, by sourcing the script
instead of executing it.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BENCH_DIR / "tools" / "campaign_laptop.sh"

STARTUP_BANNER = "2026-08-19T16:47:22.905854Z  INFO Memory limit set to 2147483648 bytes"
REAL_ALLOCATION_ERROR = ("2026-08-19T16:49:02.101010Z ERROR cannot allocate memory "
                         "for document, skipping")


def sourced(snippet: str, *args: str) -> subprocess.CompletedProcess:
    """Run a snippet with the script's functions and settings in scope."""
    command = f'source "{SCRIPT}" {" ".join(args)}\n{snippet}'
    return subprocess.run(["bash", "-c", command], cwd=BENCH_DIR,
                          capture_output=True, text=True)


def dry_run(*args: str) -> str:
    """Both streams: each repetition's body is piped through tee, which puts it
    on stdout, while the campaign's own progress lines go to stderr."""
    result = subprocess.run(["bash", str(SCRIPT), "--dry-run", *args],
                            cwd=BENCH_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def setting(name: str, *args: str) -> str:
    result = sourced(f'echo "${name}"', *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_smoke_lowers_the_build_sample_floor_it_cannot_reach():
    """A 20k-doc build finishes in about five seconds, so the campaign's floor of
    twenty one-second samples fails it on run length alone — which is what
    aborted every repetition of the first smoke run before C3-C8 were reached."""
    campaign = int(setting("MIN_BUILD_SAMPLES"))
    smoke = int(setting("MIN_BUILD_SAMPLES", "--smoke"))
    assert campaign >= 20
    assert 0 < smoke < campaign


def test_the_allocation_gate_ignores_the_vector_stores_startup_banner():
    """The vector-store logs its configured limit at INFO on every healthy run.
    Counting that line as an allocation failure makes the gate fail identically
    whether or not a document was skipped."""
    result = sourced(
        f'printf "%s\\n" "{STARTUP_BANNER}" '
        f'| grep -Ec "$VECTOR_STORE_ALLOCATION_ERROR" || true')
    assert result.stdout.strip() == "0", result.stdout


def test_the_allocation_gate_still_catches_a_real_skipped_document():
    """The gate exists because a partially-indexed corpus still answers queries
    (SIZING.md), so it has to fire on the error that says a document was
    skipped."""
    result = sourced(
        f'printf "%s\\n%s\\n" "{STARTUP_BANNER}" "{REAL_ALLOCATION_ERROR}" '
        f'| grep -Ec "$VECTOR_STORE_ALLOCATION_ERROR" || true')
    assert result.stdout.strip() == "1", result.stdout


def test_a_failed_repetition_makes_the_campaign_exit_non_zero():
    """Otherwise a campaign whose every repetition aborted prints "complete" and
    exits 0, and the plotting step runs against truncated artifacts."""
    result = sourced('FAILED_REPS=("opensearch rep 1 — log"); report_and_exit 4')
    assert result.returncode == 1
    assert "INCOMPLETE" in result.stderr
    assert "opensearch rep 1" in result.stderr


def test_a_clean_campaign_reports_complete_and_exits_zero():
    result = sourced('FAILED_REPS=(); report_and_exit 4')
    assert result.returncode == 0
    assert "complete: 4/4" in result.stderr


@pytest.mark.parametrize("config", ["opensearch", "opensearch-refresh30",
                                    "scylla-bootstrap", "scylla-cdc"])
def test_every_repetition_leaves_no_engine_running(config):
    """A gate aborts by exiting, which skips the config's closing down target.
    Without the teardown two engines are up at once, and one engine at a time is
    the premise the whole campaign rests on."""
    assert "all_stacks_down" in dry_run("--configs", config, "--reps", "1")


@pytest.mark.parametrize("config", ["opensearch", "opensearch-refresh30"])
def test_each_opensearch_configuration_names_its_own_artifacts(config):
    """The two OpenSearch configurations differ only in refresh_interval; sharing
    an artifact name would let the second overwrite the first."""
    assert f"OS_CONFIG={config}" in dry_run("--configs", config, "--reps", "1")


def test_preflight_archives_a_previous_campaigns_artifacts(tmp_path):
    """Both a 20k-doc smoke series and a 270k-doc campaign series match
    c1-opensearch-*.jsonl. Mixing them puts two different runs on one line, and
    the chart cannot say which."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "c1-opensearch-1.jsonl").write_text("{}\n", encoding="utf-8")
    (data / "manifest-opensearch-1.json").write_text("{}\n", encoding="utf-8")
    (data / "queries.json").write_text("{}\n", encoding="utf-8")

    result = sourced(f'cd "{tmp_path}" && archive_previous_artifacts')
    assert result.returncode == 0, result.stderr

    survivors = sorted(item.name for item in data.iterdir() if item.is_file())
    assert survivors == ["queries.json"], "the query set is an input, not a result"
    archived = sorted(item.name for item in (data / "superseded").glob("*/*"))
    assert archived == ["c1-opensearch-1.jsonl", "manifest-opensearch-1.json"]


@pytest.mark.parametrize("config", ["scylla-bootstrap", "scylla-cdc"])
def test_each_scylla_path_names_its_own_artifacts(config):
    """C4, C5 and C7 are the same targets on both paths, so the CDC repetitions
    ran after the bootstrap ones and overwrote them silently."""
    assert f"SCYLLA_CONFIG={config}" in dry_run("--configs", config, "--reps", "1")


def test_dry_run_can_be_asked_for_through_the_environment():
    """DRY_RUN=1 in front of the command reads as a dry run to anyone. Ignoring
    it started a real full-corpus campaign that had to be killed by hand."""
    result = subprocess.run([str(SCRIPT), "--configs", "scylla-bootstrap",
                             "--reps", "1"],
                            cwd=BENCH_DIR, capture_output=True, text=True,
                            env={**os.environ, "DRY_RUN": "1"}, timeout=60)
    assert "SCYLLA_CONFIG=scylla-bootstrap" in result.stdout + result.stderr
    assert "docker" not in result.stdout.lower(), "a dry run must not touch docker"


def test_every_configuration_is_measured_once_before_any_is_measured_twice():
    """An eight-hour campaign gets interrupted. Configuration-major ordering
    leaves the last configurations with nothing, so no chart can be drawn at all;
    repetition-major leaves every configuration at the same N."""
    output = dry_run("--reps", "2")
    order = [line for line in output.splitlines() if ", repetition " in line]
    first_pass = [line for line in order if "repetition 1 —" in line]
    assert len(first_pass) == 4, "the first pass must cover all four configs"
    assert order[:4] == first_pass, "a config repeats before another has run once"


def series_file(path: Path, samples: int, final_docs: int) -> Path:
    """A build series shaped like build_monitor's: one header, then samples that
    climb to final_docs one interval at a time."""
    lines = ['{"record": "header", "engine": "opensearch", "interval_s": 1.0}']
    for i in range(samples):
        docs = round(final_docs * (i + 1) / samples)
        previous = round(final_docs * i / samples)
        lines.append(json.dumps({"record": "sample", "i": i,
                                 "t_elapsed_s": float(i), "docs_indexed": docs,
                                 "docs_delta": docs - previous,
                                 "docs_per_s": float(docs - previous)}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def series_gate(path: Path, expected_docs: int) -> subprocess.CompletedProcess:
    return sourced(f'assert_series_complete "{path}" {expected_docs}')


def test_a_complete_build_passes_however_few_samples_it_took():
    """The first full campaign aborted all ten OpenSearch repetitions on a floor
    of twenty samples. Every one of those series had indexed all 270,269
    documents — in fourteen seconds, at a one-second sampling interval. Failing
    that is failing an engine for being fast."""
    with tempfile.TemporaryDirectory() as tmp:
        series = series_file(Path(tmp) / "c1-opensearch-1.jsonl", 15, 270269)
        result = series_gate(series, 270269)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs_indexed=270269/270269" in result.stdout


def test_a_complete_but_coarse_build_says_so_without_failing():
    """Resolution is a disclosure, not a verdict: the C1 README has to state that
    a fourteen-second build resolves shape only at whole seconds."""
    with tempfile.TemporaryDirectory() as tmp:
        series = series_file(Path(tmp) / "c1-opensearch-1.jsonl", 15, 270269)
        result = series_gate(series, 270269)
    assert result.returncode == 0
    assert "coarse time resolution" in result.stdout


@pytest.mark.parametrize("samples", [15, 40])
def test_a_build_that_stopped_short_fails_at_any_length(samples):
    """269,889 of 270,269 documents is the failure this gate exists for, and the
    old sample-count floor could not see it: the two ScyllaDB CDC repetitions
    that lost documents ran to 97 samples and would have sailed through."""
    with tempfile.TemporaryDirectory() as tmp:
        series = series_file(Path(tmp) / "c1-scylla-cdc-3.jsonl", samples, 269889)
        result = series_gate(series, 270269)
    assert result.returncode != 0
    assert "docs_indexed=269889/270269" in result.stdout


def test_a_failed_make_aborts_the_repetition():
    """make reported `Error 1` for c1-scylla-cdc in the first campaign and the
    campaign walked on to the gates. It was caught only because that particular
    loss showed up in an index count."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "data").mkdir()
        result = sourced(f'cd "{tmp}" && CONFIG=opensearch REP=1 '
                         'make_step false; echo "REACHED"')
    assert "REACHED" not in result.stdout
    assert "ABORTED" in result.stderr
    assert result.returncode != 0


def test_named_repetitions_can_be_re_run_on_their_own():
    """Two repetitions of one configuration were lost to a loader defect. --reps
    can only ever say 1..N, which would re-measure the three that are valid."""
    output = dry_run("--configs", "scylla-cdc", "--rep-list", "1,3")
    visited = [line for line in output.splitlines() if ", repetition " in line]
    assert len(visited) == 2, visited
    assert "repetition 1 — pass 1 of 2" in visited[0]
    assert "repetition 3 — pass 2 of 2" in visited[1]


def test_several_configurations_need_no_quoting():
    """`--configs opensearch opensearch-refresh30` is what REPAIR-PLAN.md
    documents and what gets typed; the first attempt at the re-run died on
    `unknown argument: opensearch-refresh30`."""
    output = dry_run("--configs", "opensearch", "opensearch-refresh30",
                     "--reps", "1")
    assert "config opensearch, repetition 1" in output
    assert "config opensearch-refresh30, repetition 1" in output


def test_a_quoted_configuration_list_still_works():
    output = dry_run("--configs", "opensearch opensearch-refresh30", "--reps", "1")
    assert "config opensearch-refresh30, repetition 1" in output


def test_an_empty_configuration_list_is_refused():
    result = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--configs",
                             "--reps", "1"],
                            cwd=BENCH_DIR, capture_output=True, text=True)
    assert result.returncode == 2
    assert "--configs needs at least one name" in result.stderr


def test_the_archive_step_is_scoped_to_the_repetitions_being_re_run():
    """Re-running two of five repetitions must not archive the other three."""
    output = dry_run("--configs", "scylla-cdc", "--rep-list", "1,3")
    assert "artifact(s) of repetition(s) 1, 3 of scylla-cdc" in output
    for kept in ("c5-scylla-cdc-phrase-2.jsonl", "c3-scylla-cdc-4.jsonl",
                 "c7-scylla-cdc-5.json"):
        assert kept not in output, f"{kept} would be archived and nothing rewrites it"


def test_smoke_writes_a_repetition_the_campaign_never_uses():
    """A smoke pass writes into the same data/ directory under the same names as
    the campaign, and on 2026-08-20 it overwrote four full-corpus repetitions."""
    output = dry_run("--smoke", "--configs", "opensearch")
    assert "repetition 99" in output
    assert "c1-opensearch-99.jsonl" in output
    assert "MAX_DOCS=20000" in output
