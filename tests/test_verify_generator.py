"""A gate that defaults is a guess inside the thing that exists to stop guesses.

G7 answers one question about a build-rate point: was that the engine's ceiling
or the client's? It answers it from three measured quantities — the point's
operations/s against the loader's measured ceiling at THAT batch level, the
harness box's CPU against the loader's own cpuset, and the busiest single
thread against one core. The last is the clause box CPU cannot supply: the
loader that was proven to be the constraint sat at 13% of the box with 77
threads in futex, and `generator_probe.GIL_NOTE` records a GIL-bound loader at
~0.80 of one core.

The batch axis is what makes the missing-ceiling case ordinary rather than
exotic: the sweep runs five levels, and until Phase 0 has calibrated the client
at each of them, most levels have no ceiling to be judged against. Passing
those on the strength of a neighbouring level's figure would publish a client
ceiling as an engine ceiling, so a level with no measurement is a refusal with
its own exit code — not a pass, and not a failure either.
"""
import json
from pathlib import Path

import pytest

from ftsbench import verify_generator

CALIBRATION = (Path(__file__).resolve().parent.parent / "tools"
               / "client_calibration.sh")

CEILINGS = {"engine": "opensearch",
            "measured_on": "fts-harness i8g.2xlarge",
            "ops_per_s": {"16": 1863.0, "64": 500.0, "512": None},
            "loader_core_bound_at": 0.70}


def ceilings_file(tmp_path, **overrides):
    document = {**CEILINGS, **overrides}
    path = tmp_path / "client-ceilings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def point_label(concurrency: int, batch: int) -> str:
    """The label `tools/sweep_build_rate.sh` writes for one point of a sweep."""
    return (f"build-rate sweep, opensearch-ramindex, "
            f"concurrency={concurrency} batch={batch}")


def probe_series(tmp_path, batch: int = 16, concurrency: int = 64,
                 box_cores: float = 2.5, thread_cores: float = 0.05,
                 ticks: int = 3, label: str | None = None,
                 name: str | None = None) -> str:
    """A generator_probe series: the box records carry the CPU denominator and
    the box level, the loader records carry the per-thread level."""
    lines = [json.dumps({
        "record": "header", "schema_version": 1,
        "producer": "generator_probe", "engine": "opensearch",
        "engine_version": "n/a",
        "label": point_label(concurrency, batch) if label is None else label,
        "cache_state": "warm-container-fresh-index",
        "corpus": "", "max_docs": 0})]
    for i in range(ticks):
        lines.append(json.dumps({
            "record": "generator_box_sample", "i": i,
            "cores_available": 8, "cores_source": "loader-cpus-allowed",
            "cpu_cores_used": box_cores, "steal_cores": 0.0}))
        lines.append(json.dumps({
            "record": "generator_sample", "i": i, "pid": 4242,
            "threads": 9, "cpu_cores_used": thread_cores * 2,
            "busiest_thread_cores": thread_cores, "runq_wait_ratio": 0.01}))
    path = tmp_path / (name or f"gen-b{batch}-c{concurrency}.jsonl")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def gate(tmp_path, *, batch: int = 16, concurrency: int = 64,
         docs_per_s: float = 11712.0, ceilings=None, series=None,
         extra=()) -> int:
    argv = ["--cpu-series", series or probe_series(tmp_path, batch=batch,
                                                   concurrency=concurrency),
            "--batch-size", str(batch), "--concurrency", str(concurrency),
            "--achieved-docs-per-s", str(docs_per_s),
            "--ceilings", ceilings or ceilings_file(tmp_path), *extra]
    return verify_generator.main(argv)


def test_a_point_with_headroom_on_every_clause_passes(tmp_path):
    """G7's own worked example: 11,712 docs/s at batch 16 is 732 operations/s
    against a measured 1,863, which is 2.55x."""
    assert gate(tmp_path) == verify_generator.EXIT_PASS


def test_a_batch_level_with_no_measured_ceiling_is_refused(tmp_path, capsys):
    """The sweep's own levels: 512 is in the document as a placeholder and 128
    is not in it at all. Neither has been calibrated, so neither point has been
    shown to be engine-bound — and a neighbouring level's figure does not
    substitute, because the ceiling is a function of the level."""
    assert gate(tmp_path, batch=512) == verify_generator.EXIT_REFUSED
    message = capsys.readouterr().err
    assert "no measured operations/s ceiling for --batch-size 512" in message
    assert "Phase 0" in message


def test_a_refusal_is_not_a_pass_and_says_so(tmp_path, capsys):
    assert gate(tmp_path, batch=512) != verify_generator.EXIT_PASS
    assert "not a pass" in capsys.readouterr().err


def test_a_null_placeholder_is_not_named_as_a_level_the_document_carries(tmp_path,
                                                                        capsys):
    """Listing 512 among the measured levels would describe a placeholder as a
    measurement, and would send a reader looking for a number that is not
    there."""
    gate(tmp_path, batch=128)
    carried = capsys.readouterr().err
    assert "it carries: 16, 64" in carried


def test_a_missing_core_bound_is_refused_rather_than_defaulted(tmp_path, capsys):
    """The 0.70 in the plan is anchored to a pre-rewrite anecdote. Defaulting to
    it would put a guess inside the CPU clause and call the result measured."""
    document = {key: value for key, value in CEILINGS.items()
                if key != "loader_core_bound_at"}
    path = tmp_path / "no-bound.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert gate(tmp_path, ceilings=str(path)) == verify_generator.EXIT_REFUSED
    assert "no loader_core_bound_at" in capsys.readouterr().err


def test_a_point_too_close_to_its_client_ceiling_is_client_bound(tmp_path, capsys):
    """20,000 docs/s at batch 16 is 1,250 operations/s against 1,863 — 1.49x,
    so the client is within reach of the number and the point is a lower bound
    on the engine, drawn hollow and never plotted as an engine figure."""
    assert gate(tmp_path, docs_per_s=20000.0) == \
        verify_generator.EXIT_CLIENT_BOUND
    assert "CLIENT-BOUND: operations/s" in capsys.readouterr().err


def test_a_pinned_thread_is_caught_where_box_cpu_alone_would_pass(tmp_path,
                                                                  capsys):
    """One thread at a full core on a box sitting at 2.5 of 8: the box clause
    clears easily and the point is still the client's. This is the pathology
    the gate exists for."""
    series = probe_series(tmp_path, thread_cores=1.0)
    assert gate(tmp_path, series=series) == verify_generator.EXIT_CLIENT_BOUND
    assert "busiest thread" in capsys.readouterr().err


def test_exactly_twice_the_ceiling_clears(tmp_path):
    """The margin is inclusive, so the boundary is a decision rather than an
    accident of comparison."""
    at_margin = 1863.0 * 16 / verify_generator.REQUIRED_MARGIN
    assert gate(tmp_path, docs_per_s=at_margin) == verify_generator.EXIT_PASS


def test_a_series_measured_over_another_point_is_refused(tmp_path, capsys):
    """The label carries `batch=<n>` because the sweep put it there. A gate run
    against the wrong point's generator series would otherwise return a
    plausible verdict about a point nobody asked about."""
    series = probe_series(tmp_path, batch=64)
    assert gate(tmp_path, batch=16, series=series) == \
        verify_generator.EXIT_REFUSED
    assert "another point's generator series" in capsys.readouterr().err


def test_a_series_from_another_rung_of_the_same_sweep_is_refused(tmp_path,
                                                                 capsys):
    """One sweep runs a whole concurrency ladder at one batch level, so `batch=`
    agrees on every rung and only `concurrency=` says which point a series
    covers. Handed the c=8 warm-up's probe, the gate returned PASS about a point
    whose own series proves it client-bound."""
    warmup = probe_series(tmp_path, batch=16, concurrency=8)
    assert gate(tmp_path, batch=16, concurrency=64, series=warmup) == \
        verify_generator.EXIT_REFUSED
    message = capsys.readouterr().err
    assert "concurrency=8" in message
    assert "another point's generator series" in message


def test_a_probe_over_a_whole_pass_cannot_stand_in_for_one_point(tmp_path,
                                                                 capsys):
    """A probe run once over a multi-point pass carries a label that names no
    rung at all, so nothing in it says the window covers the point being
    judged. Clearing on that would be a verdict about every point at once,
    which is a verdict about none of them."""
    unsliced = probe_series(tmp_path, label="phase0 client calibration",
                            name="gen-whole-pass.jsonl")
    assert gate(tmp_path, series=unsliced) == verify_generator.EXIT_REFUSED
    message = capsys.readouterr().err
    assert "names no concurrency" in message
    assert "Slice the generator series" in message, \
        "the refusal must say what to do, not only that it refused"


def test_the_concurrency_the_point_ran_at_is_owed_rather_than_guessed(tmp_path):
    """It is not read off the file name: a name is a label and the header is the
    record, which is the rule the batch axis keeps for its own per-level
    directories. The batch clause already compares the label against a required
    flag, and this is the same clause about the other half of the point."""
    argv = ["--cpu-series", probe_series(tmp_path), "--batch-size", "16",
            "--achieved-docs-per-s", "11712",
            "--ceilings", ceilings_file(tmp_path)]
    with pytest.raises(SystemExit) as refused:
        verify_generator.parse_args(argv)
    assert refused.value.code == 2, "argparse owns the usage exit"


def test_a_series_with_no_cpu_samples_is_refused_rather_than_passed(tmp_path,
                                                                   capsys):
    """A probe that wrote fewer than two ticks records no rate at all. Absent
    evidence is the one thing that must never read as clearance."""
    header_only = tmp_path / "gen-empty.jsonl"
    header_only.write_text(
        json.dumps({"record": "header", "producer": "generator_probe",
                    "label": "concurrency=64 batch=16"}) + "\n",
        encoding="utf-8")
    assert gate(tmp_path, series=str(header_only)) == \
        verify_generator.EXIT_REFUSED
    assert "cpu_cores_used" in capsys.readouterr().err


def test_an_unreadable_ceilings_document_is_refused(tmp_path, capsys):
    assert gate(tmp_path, ceilings=str(tmp_path / "nothing.json")) == \
        verify_generator.EXIT_REFUSED
    assert "missing or unparseable" in capsys.readouterr().err


def test_the_refusal_code_is_distinct_from_both_verdicts():
    """A caller has to be able to tell "not shown to be engine-bound" from
    "shown to be client-bound", and argparse already owns 2."""
    codes = {verify_generator.EXIT_PASS, verify_generator.EXIT_CLIENT_BOUND,
             verify_generator.EXIT_REFUSED}
    assert len(codes) == 3
    assert 2 not in codes


def test_a_pass_prints_the_figures_a_gate_log_can_record(tmp_path, capsys):
    """The `observed:` line is passed straight to `ftsbench.gate_log
    --observed`, so a recorded gate carries the numbers rather than the word."""
    gate(tmp_path)
    printed = capsys.readouterr().out
    assert "observed:" in printed
    assert "732" in printed, "the operations/s figure is not in the record"


def test_the_only_caller_names_the_point_the_gate_checks():
    """`tools/client_calibration.sh` is the only thing that runs this gate, and
    both halves of "which point is this?" live there: the label its probe
    writes and the flags its generated gate command passes. The label used to
    name the point in a form of its own (`-b16-c8-`) and the command passed no
    concurrency at all, so nothing could be compared with anything."""
    text = CALIBRATION.read_text(encoding="utf-8")
    invocation = text.split("-m ftsbench.verify_generator", 1)[1]
    command = invocation.split("CMD", 1)[0]
    assert "--concurrency" in command and "--batch-size" in command
    labels = [line for line in text.splitlines()
              if "concurrency=" in line and "batch=" in line]
    assert labels, "no probe label names the point in the gate's own form"
