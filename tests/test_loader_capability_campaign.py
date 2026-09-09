"""The loader-capability campaign: six literal arms, one arm's ladder, and the
gates on a point.

Two levels, stubbed two ways, because they answer different questions.

The campaign is a list of six invocations, so what is worth testing is not its
arithmetic but that the lines say what the plan decided. The ladder is stubbed
out with the same replace-one-file-in-a-symlink-tree pattern as
tests/test_build_rate_campaign.py.

The ladder's own tests stub `$PYTHON` instead — every loader, probe and corpus
command goes through it, so the stub can record the argv and leave behind
artifacts shaped like the real ones. That reaches the gates, and the gate the
whole campaign turns on is G1: `generator_probe` finds loaders by `-m <module>`
adjacency, so a run whose loaders it cannot see writes a complete, plausible
CPU series full of zeros that every other gate passes. G1 gets a positive and a
negative example rather than a smoke check.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent
CAMPAIGN = BENCH_DIR / "tools" / "loader_capability_campaign.sh"
SWEEP = BENCH_DIR / "tools" / "loader_capability_sweep.sh"

ARMS = (("opensearch", "4"), ("opensearch", "6"), ("opensearch", "8"),
        ("scylladb", "4"), ("scylladb", "6"), ("scylladb", "8"))
LADDER = "24 48 96 192 384"

SWEEP_STUB = r"""#!/usr/bin/env bash
# Records the arm and every knob the campaign line set for this run.
printf 'run engine=%q workers=%q reps=%q OUT_DIR=%q LADDER=%q DOCS_OS=%q DOCS_SCYLLA=%q OS_BATCH=%q PROBE_INTERVAL=%q SINK_HOST=%q HTTP_PORTS=%q CQL_PORTS=%q WARMUP=%q DRY_RUN=%q\n' \
  "$1" "$2" "${3:-}" "${OUT_DIR:-}" "${LADDER:-}" "${DOCS_OS:-}" "${DOCS_SCYLLA:-}" \
  "${OS_BATCH:-}" "${PROBE_INTERVAL:-}" "${SINK_HOST:-}" "${HTTP_PORTS:-}" \
  "${CQL_PORTS:-}" "${WARMUP:-}" "${DRY_RUN:-}" >> "$SWEEP_LOG"
exit "${SWEEP_EXIT:-0}"
"""


def stub_tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "tools").mkdir(parents=True)
    for entry in BENCH_DIR.iterdir():
        if entry.name != "tools":
            (root / entry.name).symlink_to(entry)
    for entry in (BENCH_DIR / "tools").iterdir():
        if entry.name != SWEEP.name:
            (root / "tools" / entry.name).symlink_to(entry)
    sweep = root / "tools" / SWEEP.name
    sweep.write_text(SWEEP_STUB, encoding="utf-8")
    sweep.chmod(0o755)
    return root


def run_campaign(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tree = stub_tree(tmp_path)
    sweep_log = tmp_path / "sweep.log"
    sweep_log.touch()
    environment = {
        **os.environ,
        "SWEEP_LOG": str(sweep_log),
        "ROOT": str(tmp_path / "artifacts"),
        **(env or {}),
    }
    result = subprocess.run(
        ["bash", str(tree / "tools" / CAMPAIGN.name), *args],
        cwd=tree, capture_output=True, text=True, env=environment, timeout=120)
    result.runs = [dict(token.split("=", 1) for token in shlex.split(line)[1:])
                   for line in sweep_log.read_text(encoding="utf-8").splitlines()
                   if line.startswith("run ")]
    return result


def runs(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    result = run_campaign(tmp_path, *(args or ("run",)), env=env)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.runs, "the campaign ran nothing"
    return result.runs


# --- the campaign: six lines ----------------------------------------------

def test_both_scripts_still_parse():
    for script in (CAMPAIGN, SWEEP):
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0, script


@pytest.mark.parametrize("verb", ["", "list", "--help", "sweep", "RUN"])
def test_nothing_but_the_three_verbs_starts_the_campaign(tmp_path, verb):
    """There is no default verb: the cheapest way to spend a fleet session by
    accident is a script that measures when it is asked for a listing."""
    result = run_campaign(tmp_path, *([verb] if verb else []))
    assert result.returncode == 2, result.stdout
    assert result.runs == []


def test_the_roster_is_two_engines_by_three_worker_counts(tmp_path):
    """One dedicated chart line per arm. A missing arm is a line the chart
    silently does not have."""
    ordered = tuple((run["engine"], run["workers"]) for run in runs(tmp_path))
    assert ordered == ARMS, ordered


def test_opensearch_n4_runs_first(tmp_path):
    """It is the arm the build-rate campaign's own gate needs, and it is the
    pilot: P0 measured this client flat in concurrency to within +-3% at N=1,
    so if that reproduces the five lines after it are five more flat curves and
    the x axis is the thing to settle first."""
    first = runs(tmp_path)[0]
    assert (first["engine"], first["workers"]) == ("opensearch", "4")


def test_the_pilot_runs_the_first_line_only_at_one_repetition(tmp_path):
    """Ten minutes to answer whether the ladder has a shape — which one
    repetition shows — before the other five arms are run against the same
    question."""
    pilot = runs(tmp_path, "pilot")
    assert len(pilot) == 1, pilot
    assert (pilot[0]["engine"], pilot[0]["workers"]) == ("opensearch", "4")
    assert pilot[0]["reps"] == "1", pilot[0]
    assert pilot[0]["WARMUP"] == "0", "a pilot asking for shape needs no warm-up"


def test_every_arm_gets_the_same_ladder_reps_and_warm_up(tmp_path):
    """No line is quietly less certain than its neighbours: a curve drawn from
    a mixture of N=3 and N=1 points has a spread on some markers and not on
    others, and nothing on the chart says which."""
    for run in runs(tmp_path):
        assert run["LADDER"] == LADDER, run
        assert run["reps"] == "3", run
        assert run["WARMUP"] == "1", run
    for run in runs(tmp_path / "override", env={"REPS": "5"}):
        assert run["reps"] == "5", run


def test_every_rung_divides_by_every_worker_count(tmp_path):
    """A worker's share of the concurrency is remainder-preserving, so on an
    unbalanced rung some workers hold one more operation than others and the
    point's rate is dragged by whichever shard drew the short share — a
    spurious slope on the axis being plotted."""
    for run in runs(tmp_path):
        assert run["LADDER"] == LADDER, run
    for rung in (int(rung) for rung in LADDER.split()):
        for _, workers in ARMS:
            assert rung % int(workers) == 0, (rung, workers)


def test_no_rung_is_below_the_largest_worker_count(tmp_path):
    """A share floors at 1, so a total below the worker count would offer the
    worker count: a c=4 point at N=8 offers 8 and the chart would say 4. The
    divisibility rule above already forces this, and it is asserted separately
    because it is the one that makes the x axis mean what it says."""
    largest = max(int(workers) for _, workers in ARMS)
    for run in runs(tmp_path):
        assert min(int(rung) for rung in run["LADDER"].split()) >= largest, run


def test_every_arm_offers_the_locked_batch_and_document_budgets(tmp_path):
    """OpenSearch 512 is "Decisions locked"; with OS_BATCH unset the loader
    falls back to its own default, which is not the decision. The caps are
    sized so a point outlasts the probe ticks client_ceilings discards."""
    for run in runs(tmp_path):
        assert run["OS_BATCH"] == "512", run
        assert run["DOCS_OS"] == "1200000", run
        assert run["DOCS_SCYLLA"] == "400000", run
        assert run["PROBE_INTERVAL"] == "0.5", run


def test_every_arm_is_given_two_sink_instances_per_mode(tmp_path):
    """The sink is one asyncio loop in one process, and P0's N=4 OpenSearch
    point already pushed ~400 MB/s of _bulk bodies through one. Every loader
    behind a single instance would measure the sink."""
    for run in runs(tmp_path):
        assert len(run["HTTP_PORTS"].split()) == 2, run
        assert len(run["CQL_PORTS"].split()) == 2, run


def test_every_arm_writes_into_one_reduced_directory(tmp_path):
    """client_ceilings reads a directory, and the grid both charts want is
    every arm's points in one place."""
    assert {run["OUT_DIR"] for run in runs(tmp_path)} == \
        {str(tmp_path / "artifacts" / "points")}


def test_dry_run_reaches_every_line_without_measuring_anything(tmp_path):
    previewed = runs(tmp_path, "dry-run")
    assert len(previewed) == len(ARMS), previewed
    for run in previewed:
        assert run["DRY_RUN"] == "1", run


def test_an_arm_that_fails_stops_the_campaign(tmp_path):
    result = run_campaign(tmp_path, "run", env={"SWEEP_EXIT": "3"})
    assert result.returncode != 0, result.stdout
    assert len(result.runs) == 1, "the campaign continued past a failed arm"


def test_the_finished_campaign_names_what_turns_points_into_numbers(tmp_path):
    result = run_campaign(tmp_path, "run")
    assert result.returncode == 0, result.stderr[-2000:]
    for expected in ("ftsbench.client_ceilings", "ftsbench.plot_loader_capability",
                     "ftsbench.plot_loader_cpu", "unexpected_requests"):
        assert expected in result.stdout, expected


def test_the_campaign_warns_that_n8_is_not_a_worker_ceiling(tmp_path):
    """client_ceilings.worker_ceiling is computed over whatever ladder it
    finds. An N=8 rung that flattened because an 8-vCPU box ran out of cores
    would be published as N_max = 8 — a core count reported as a client
    property."""
    result = run_campaign(tmp_path, "run")
    assert "N_max = 8" in result.stdout
    assert "loader_core_bound_at" in result.stdout


# --- one arm's ladder, and the gates --------------------------------------

# A stub `python3`. Three jobs: record every invocation, hand the gate's own
# heredoc to the real interpreter, and leave behind artifacts the gate can read.
# LOADERS_RUNNING and STUB_TICKS let a test construct a probe series that saw
# the wrong number of loaders, or too few ticks to judge.
PYTHON_STUB = r'''#!/usr/bin/env python3
import json, os, shlex, sys, time

REAL = os.environ["REAL_PYTHON"]
argv = sys.argv[1:]

# `$PYTHON - <<'GATE'` — the sweep's own gate, which must really run.
if argv and argv[0] == "-":
    os.execv(REAL, [REAL] + argv)

with open(os.environ["PY_LOG"], "a", encoding="utf-8") as log:
    log.write(shlex.join(argv) + "\n")


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def write(path, records):
    with open(path, "w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


module = flag("-m")
if module == "ftsbench.synth_corpus":
    stem = flag("--output")[: -len(".jsonl")]
    for shard in range(int(flag("--shards", "1"))):
        write(f"{stem}-{shard}.jsonl", [{"id": "1", "title": "t", "text": "x"}])
elif module == "ftsbench.generator_probe":
    ticks = int(os.environ.get("STUB_TICKS", "10"))
    workers = int(os.environ["STUB_WORKERS"])
    running = int(os.environ.get("LOADERS_RUNNING", "0")) or workers
    records = [{"record": "header", "schema_version": 1,
                "producer": "generator_probe", "engine": flag("--engine", ""),
                "label": flag("--label", "")}]
    for i in range(ticks):
        rate = None if i == 0 else 0.92
        for pid in range(workers):
            records.append({"record": "generator_sample", "i": i,
                            "pid": 1000 + pid, "t_elapsed_s": i * 0.5,
                            "t_unix_s": 1.0 + i * 0.5, "cpu_cores_used": rate,
                            "busiest_thread_cores": rate, "threads": 2,
                            "running": True})
        records.append({"record": "generator_box_sample", "i": i,
                        "t_elapsed_s": i * 0.5, "t_unix_s": 1.0 + i * 0.5,
                        "cores_available": 8,
                        "cpu_cores_used": None if i == 0 else 0.92 * workers,
                        "steal_cores": None if i == 0 else 0.0,
                        "swap_used_bytes": 0, "loaders_running": running})
    write(flag("--output"), records)
elif module in ("ftsbench.opensearch_load", "ftsbench.scylla_load"):
    # No real loader exits before the probe has written a tick, and the sweep
    # stops the probe at the first exit. Without a floor here the stub loaders
    # finish in microseconds and the probe is killed mid-file, which fails
    # whichever gate reads the series next.
    time.sleep(float(os.environ.get("STUB_LOADER_SECONDS", "0.3")))
    per_op = int(flag("--batch-size", "1"))
    cap = int(flag("--max-docs")) - int(os.environ.get("STUB_SHORT_BY", "0"))
    records = [{"record": "header", "schema_version": 1, "producer": "loader",
                "engine": "stub", "concurrency": int(flag("--concurrency")),
                "batch_size": per_op, "label": flag("--label", "")}]
    delivered = i = 0
    while delivered < cap:
        docs = min(per_op, cap - delivered)
        delivered += docs
        records.append({"record": "latency_op", "i": i, "t_start_s": i * 0.001,
                        "t_end_s": 4.0 + i * 0.001, "latency_ms": 1.0,
                        "service_ms": 1.0, "op": "insert", "n_docs": docs,
                        "ok": True, "error": None})
        i += 1
    write(flag("--latency-log"), records)
'''

# Reps is positional on the sweep, so it is passed as an argument rather than
# put in here.
ONE_POINT = {"LADDER": "24", "WARMUP": "0", "DOCS_OS": "2048",
             "STUB_WORKERS": "4"}


def run_sweep(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "stub-python3"
    stub.write_text(PYTHON_STUB, encoding="utf-8")
    stub.chmod(0o755)
    py_log = tmp_path / "python.log"
    py_log.touch()
    root = tmp_path / "artifacts"
    result = subprocess.run(
        ["bash", str(SWEEP), *args],
        cwd=BENCH_DIR, capture_output=True, text=True, timeout=300,
        env={**os.environ, "ROOT": str(root), "PYTHON": str(stub),
             "REAL_PYTHON": sys.executable, "PY_LOG": str(py_log),
             **(env or {})})
    result.root = root
    result.points = root / "points"
    result.invocations = [shlex.split(line) for line
                          in py_log.read_text(encoding="utf-8").splitlines()]
    return result


def one_point(tmp_path: Path, **extra: str):
    result = run_sweep(tmp_path, "opensearch", "4", "1",
                       env={**ONE_POINT, **extra})
    assert result.returncode == 0, result.stderr[-3000:]
    return result


def aside(result) -> list[str]:
    return sorted(path.name for path in result.points.glob("*.failed"))


def test_a_measured_point_passes_every_gate(tmp_path):
    """The positive example. Without one, a gate that always fails and a gate
    that always passes look identical from the sweep's exit code."""
    result = one_point(tmp_path)
    assert not (result.root / "failed-points.log").exists(), result.stderr[-3000:]
    assert sorted(path.name for path in result.points.glob("*.jsonl")) == [
        "gen-opensearch-b512-c24-w4-r1.jsonl",
        "lat-opensearch-b512-c24-w4-r1-s0.jsonl",
        "lat-opensearch-b512-c24-w4-r1-s1.jsonl",
        "lat-opensearch-b512-c24-w4-r1-s2.jsonl",
        "lat-opensearch-b512-c24-w4-r1-s3.jsonl"]


def test_the_artifacts_are_named_so_the_existing_reducer_finds_them(tmp_path):
    """client_ceilings.POINT_RE is lat-<engine>-b<n>-c<n>-w<n>-r<n>-s<shard>.jsonl
    and PointKey.probe_name() is gen-<engine>-b<n>-c<n>-w<n>-r<n>.jsonl. Matching
    both is what makes this campaign need no reducer of its own."""
    from ftsbench import client_ceilings

    result = one_point(tmp_path)
    points = client_ceilings.collect(result.points)
    assert len(points) == 1, points
    point = points[0]
    assert (point.key.engine, point.key.batch, point.key.concurrency,
            point.key.workers, point.key.rep) == ("opensearch", 512, 24, 4, 1)
    assert (point.shards, point.ok_docs, point.errors) == (4, 2048, 0)
    assert point.cost is not None, "the probe series did not reduce"
    assert point.cost.cores_available == 8
    assert point.docs_per_s > 0


def test_a_probe_that_did_not_see_the_loaders_sets_the_point_aside(tmp_path):
    """G1, the gate this campaign turns on. generator_probe matches loaders by
    `-m <module>` adjacency; against a spawned worker pool it finds only the
    idle parent, and the series it writes is complete, plausible and about
    nothing. Every other gate passes on it."""
    result = one_point(tmp_path, LOADERS_RUNNING="1")
    assert "G1 loaders_running" in result.stderr, result.stderr[-3000:]
    assert len(aside(result)) == 5, aside(result)
    log = (result.root / "failed-points.log").read_text(encoding="utf-8")
    assert "opensearch-b512-c24-w4-r1" in log, log


def test_a_point_too_short_to_carry_a_cpu_figure_is_set_aside(tmp_path):
    """G3. client_ceilings discards the first two probe ticks, so a point that
    outlasted only those has no CPU figure at all — and a constant reported
    from it would be a guess in front of a gate."""
    result = one_point(tmp_path, STUB_TICKS="3")
    assert "G3 duration" in result.stderr, result.stderr[-3000:]
    assert len(aside(result)) == 5, aside(result)


def test_a_short_read_is_set_aside_rather_than_reported_as_a_rate(tmp_path):
    """G2. Dividing documents delivered by the wall of the budget that was
    offered reports a truncated run as a slow one."""
    result = one_point(tmp_path, STUB_SHORT_BY="512")
    assert "G2 docs" in result.stderr, result.stderr[-3000:]
    assert len(aside(result)) == 5, aside(result)


def test_a_set_aside_point_leaves_nothing_for_the_reducer(tmp_path):
    """A refused point that stayed in place is a refused point on the chart."""
    from ftsbench import client_ceilings

    result = one_point(tmp_path, LOADERS_RUNNING="1")
    assert client_ceilings.collect(result.points) == []


def test_the_document_budget_is_split_so_the_shares_sum_to_it(tmp_path):
    """--max-docs is per process here. A point that loaded budget/N documents
    would report a rate over the wrong denominator, and G2 would refuse it for
    an arithmetic reason rather than an engine one."""
    result = one_point(tmp_path)
    caps = [int(argv[argv.index("--max-docs") + 1]) for argv in result.invocations
            if "ftsbench.opensearch_load" in argv]
    assert len(caps) == 4 and sum(caps) == 2048, caps


def test_the_concurrency_is_split_and_the_label_carries_the_total(tmp_path):
    """verify_generator reads concurrency= and batch= out of the label and
    refuses a series without them; the loader's own --concurrency is this
    worker's share, which is only readable next to the run's total."""
    result = one_point(tmp_path)
    for argv in result.invocations:
        if "ftsbench.opensearch_load" not in argv:
            continue
        label = argv[argv.index("--label") + 1]
        assert "concurrency=24" in label and "batch=512" in label, label
        assert "workers=4" in label, label
        assert argv[argv.index("--concurrency") + 1] == "6", argv


def test_the_loaders_of_one_point_are_spread_over_the_sink_instances(tmp_path):
    result = one_point(tmp_path)
    urls = [argv[argv.index("--url") + 1] for argv in result.invocations
            if "ftsbench.opensearch_load" in argv]
    assert sorted(set(urls)) == ["http://127.0.0.1:9200", "http://127.0.0.1:9201"]


def test_no_scylla_loader_is_handed_a_batch_size(tmp_path):
    """One operation is one prepared INSERT; the CQL loader rejects the flag
    rather than ignoring it."""
    result = run_sweep(tmp_path, "scylladb", "4", "1",
                       env={**ONE_POINT, "DOCS_SCYLLA": "2048"})
    assert result.returncode == 0, result.stderr[-3000:]
    loaders = [argv for argv in result.invocations
               if "ftsbench.scylla_load" in argv]
    assert loaders
    for argv in loaders:
        assert "--batch-size" not in argv, argv
        assert argv[argv.index("--port") + 1] in ("9042", "9043"), argv


def test_the_warm_up_lands_outside_the_reduced_directory(tmp_path):
    """client_ceilings.POINT_RE matches r0 as readily as r1, so a warm-up left
    beside the measured points is a cold repetition inside a median."""
    from ftsbench import client_ceilings

    result = run_sweep(tmp_path, "opensearch", "4", "1",
                       env={**ONE_POINT, "WARMUP": "1"})
    assert result.returncode == 0, result.stderr[-3000:]
    assert sorted(path.name for path in
                  (result.points / "warmup").glob("gen-*.jsonl")) == [
        "gen-opensearch-b512-c24-w4-r0.jsonl"]
    reduced = client_ceilings.collect(result.points)
    assert [point.key.rep for point in reduced] == [1], reduced


def test_a_bare_invocation_says_what_it_wants(tmp_path):
    result = subprocess.run(["bash", str(SWEEP)], cwd=BENCH_DIR,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in result.stderr


def test_dry_run_prints_the_loader_argv_and_touches_nothing(tmp_path):
    result = run_sweep(tmp_path, "opensearch", "4", "1",
                       env={**ONE_POINT, "DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.invocations == [], result.invocations
    assert "ftsbench.opensearch_load" in result.stderr
    assert not list(result.points.glob("*.jsonl"))


# --- the probe stops where the measured window ends ------------------------

# The static stub above writes its whole probe series at once, so it cannot
# express when the probe was stopped. This one runs like the real probe: it
# ticks until SIGTERM, counting the loaders that are still alive from marker
# files the stub loaders keep. Shard 0 finishes long before its siblings, so a
# probe that waits for the last loader records the drain and one that stops at
# the first exit does not.
DRAIN_STUB = r'''#!/usr/bin/env python3
import json, os, signal, sys, time

REAL = os.environ["REAL_PYTHON"]
argv = sys.argv[1:]
if argv and argv[0] == "-":
    os.execv(REAL, [REAL] + argv)


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


RUNDIR = os.environ["STUB_RUNDIR"]
WORKERS = int(os.environ["STUB_WORKERS"])
module = flag("-m")

if module == "ftsbench.synth_corpus":
    stem = flag("--output")[: -len(".jsonl")]
    for shard in range(int(flag("--shards", "1"))):
        with open(f"{stem}-{shard}.jsonl", "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": "1", "title": "t", "text": "x"}) + "\n")

elif module == "ftsbench.generator_probe":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with open(flag("--output"), "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"record": "header", "schema_version": 1,
                                 "producer": "generator_probe",
                                 "engine": flag("--engine", ""),
                                 "label": flag("--label", "")}) + "\n")
        stream.flush()
        for i in range(400):
            alive = len([n for n in os.listdir(RUNDIR) if n.endswith(".alive")])
            rate = None if i == 0 else 0.92
            for pid in range(WORKERS):
                stream.write(json.dumps(
                    {"record": "generator_sample", "i": i, "pid": 1000 + pid,
                     "t_elapsed_s": i * 0.1, "t_unix_s": 1.0 + i * 0.1,
                     "cpu_cores_used": rate, "busiest_thread_cores": rate,
                     "threads": 2, "running": True}) + "\n")
            stream.write(json.dumps(
                {"record": "generator_box_sample", "i": i,
                 "t_elapsed_s": i * 0.1, "t_unix_s": 1.0 + i * 0.1,
                 "cores_available": 8,
                 "cpu_cores_used": None if i == 0 else 0.92 * WORKERS,
                 "steal_cores": None if i == 0 else 0.0,
                 "swap_used_bytes": 0, "loaders_running": alive}) + "\n")
            stream.flush()
            time.sleep(0.1)

elif module in ("ftsbench.opensearch_load", "ftsbench.scylla_load"):
    log = flag("--latency-log")
    shard = int(log[log.rindex("-s") + 2: -len(".jsonl")])
    marker = os.path.join(RUNDIR, f"loader-{shard}.alive")
    open(marker, "w").close()
    # Shard 0 is the early finisher whose exit ends the measured window. It has
    # to outlast the warm-in plus MIN_TICKS -- (2 + 5) * 0.5s -- or the point is
    # refused for being short rather than kept or refused for the drain.
    time.sleep(4.0 if shard == 0 else 8.0)
    per_op = int(flag("--batch-size", "1"))
    cap = int(flag("--max-docs"))
    with open(log, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"record": "header", "schema_version": 1,
                                 "producer": "loader", "engine": "stub",
                                 "concurrency": int(flag("--concurrency")),
                                 "batch_size": per_op,
                                 "label": flag("--label", "")}) + "\n")
        delivered = i = 0
        while delivered < cap:
            docs = min(per_op, cap - delivered)
            delivered += docs
            stream.write(json.dumps(
                {"record": "latency_op", "i": i, "t_start_s": 0.0,
                 "t_end_s": 4.0, "latency_ms": 1.0, "service_ms": 1.0,
                 "op": "insert", "n_docs": docs, "ok": True,
                 "error": None}) + "\n")
            i += 1
    # Last thing, and then straight out: the real probe counts processes, which
    # vanish at exit. A marker dropped before interpreter teardown would let the
    # probe see a short count while this loader is still a live child, which is
    # a race in the stub and not in the thing under test.
    os.remove(marker)
    os._exit(0)
'''


def run_drain_sweep(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "drain-python3"
    stub.write_text(DRAIN_STUB, encoding="utf-8")
    stub.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    root = tmp_path / "artifacts"
    result = subprocess.run(
        ["bash", str(SWEEP), "opensearch", "4", "1"],
        cwd=BENCH_DIR, capture_output=True, text=True, timeout=300,
        env={**os.environ, "ROOT": str(root), "PYTHON": str(stub),
             "REAL_PYTHON": sys.executable, "STUB_RUNDIR": str(rundir),
             "LADDER": "24", "WARMUP": "0", "DOCS_OS": "2048",
             "PROBE_INTERVAL": "0.5", "STUB_WORKERS": "4"})
    result.root = root
    result.points = root / "points"
    return result


def drain_series(points: Path) -> list[int]:
    """The ticks the gate judges: the first two are the loaders coming up, and
    client_ceilings.WARM_IN_TICKS drops them at both the gate and the reducer."""
    from ftsbench import client_ceilings
    path = next(points.glob("gen-*.jsonl"))
    counts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if (record.get("record") == "generator_box_sample"
                and (record.get("i") or 0) >= client_ceilings.WARM_IN_TICKS):
            counts.append(record["loaders_running"])
    return counts


@pytest.fixture(scope="module")
def drain_run(tmp_path_factory):
    """One staggered run, asserted from two angles: the recorded series and the
    gate's verdict on it. Module-scoped because it sleeps eight seconds."""
    return run_drain_sweep(tmp_path_factory.mktemp("drain"))


def test_the_probe_stops_at_the_first_loader_exit_not_the_last(drain_run):
    """The drain is not part of the measured window. Before this, the sweep
    waited for every loader and then stopped the probe, so the trailing ticks
    counted fewer than N loaders — which G1 refuses and which drags the CPU
    median toward an idle box. 45 of 47 refusals on the 2026-09-09 fleet pass
    were this and nothing else."""
    result = drain_run
    assert result.returncode == 0, result.stderr[-3000:]
    counts = drain_series(result.points)
    assert counts, "the probe recorded no box ticks"
    assert set(counts) == {4}, (
        f"the probe recorded the drain: {counts[-8:]}")


def test_a_point_whose_shards_finish_apart_is_not_refused_for_the_drain(drain_run):
    """The gate's verdict, not just the series: the same staggered point must
    now pass G1 and be kept."""
    result = drain_run
    assert result.returncode == 0, result.stderr[-3000:]
    assert not list(result.points.glob("*.failed")), result.stderr[-3000:]
    assert not (result.root / "failed-points.log").exists(), result.stderr[-3000:]
