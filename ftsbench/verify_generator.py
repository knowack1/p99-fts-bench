"""Decide whether a build-rate point was the engine's ceiling or the client's.

`generator_probe` records what the load generator cost; nothing judged it. Every
"the client was the bottleneck" conclusion in this repo — the ones in
`results/client-model-2026-09-08/` included — was read off the shape of a
throughput curve, and a shape cannot separate an engine that flattened from a
loader that ran out of CPU. This is gate G1/G7 of `BUILD-RATE-MATRIX-PLAN.md`:
a point clears only when the client is demonstrably far from its own limit.

Three clauses, all of which must hold:

  - **Operation margin.** Achieved operations/s — documents/s divided by
    `--batch-size`, because N documents in one `_bulk` are one request — is at
    least 2x below the client's measured operations/s ceiling at THAT batch
    level. Batch size is the one axis on which a documents/s ceiling and an
    operations/s ceiling diverge, so the level's own figure is required and a
    neighbouring level's does not stand in for it. The 2x is
    `READ-PATH-TEST-PLAN.md`'s threshold, reused so the read and write paths do
    not carry two definitions of headroom.
  - **Box CPU.** Peak `cpu_cores_used` over the loader's own cpuset is below
    `loader_core_bound_at` x cores. Peak rather than mean, for the reason
    `verify_cpu_usage` gives: a build has a ramp and a tail, and the mean over
    both understates what was reached while it was building. The median is
    printed beside it so a one-tick spike is visible as one tick.
  - **No pinned thread.** `busiest_thread_cores` is below `loader_core_bound_at`
    of ONE core. This is the clause raw box CPU cannot supply: the loader
    measured at 13% of the box with 77 threads asleep in futex WAS the
    constraint, and `generator_probe.GIL_NOTE` records a loader proven GIL-bound
    by A/B sitting at ~0.80 of one core. The one calibrated fraction is applied
    to each entity's own budget — the box's cores, and one core for one thread.

**A missing ceiling is a refusal, not a pass.** Phase 0 owes a per-process
operations/s figure per batch level; until it runs, no cell has been shown to be
engine-bound. Defaulting the threshold, or borrowing the neighbouring level's,
would put a guess inside the mechanism that exists to keep guesses out of the
deck — which is what this campaign stopped for. `loader_core_bound_at` is owed
from the same session and is read from the ceilings document for the same
reason. A clause whose evidence is absent or all-null is refused rather than
waived, following the rule `generator_probe` states for its own nulls, and no
clause in this module can report `ok` from a measurement it does not have.

**The series has to be the point's own.** `--batch-size` and `--concurrency`
together name the point, and both are checked against the `batch=` and
`concurrency=` tokens the sweep writes into the series label. A sweep runs a
whole concurrency ladder at one batch level, so the batch token agrees on every
rung and only the concurrency token says which rung a series covers; without
that check a neighbouring rung's probe — the c=8 warm-up's, measured while the
loader was nearly idle — cleared a point that its own series proves
client-bound. A label carrying neither token is a probe over a whole pass and is
refused too: a verdict about every point at once is a verdict about none of
them.

Exit codes: **0** clear, **1** client-bound (a clause was evaluated and failed),
**3** refused (the evidence to evaluate a clause is owed). 2 is argparse's own
usage exit and is left to it.

    .venv/bin/python3 -m ftsbench.verify_generator \\
        --cpu-series data/sweep/b16/gen-opensearch-ramindex-c64-1.jsonl \\
        --batch-size 16 --concurrency 64 --achieved-docs-per-s 11712 \\
        --ceilings tuning/client-ceilings-opensearch.json

The verdict is recorded with `ftsbench.gate_log --name generator_headroom`; the
`observed:` line this prints is written to be passed straight to its
`--observed`, so the manifest carries the numbers and not just the word.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import runmeta

BOX_RECORD = "generator_box_sample"
LOADER_RECORD = "generator_sample"
LOADER_CORES_SOURCE = "loader-cpus-allowed"

# READ-PATH-TEST-PLAN.md:51, and not a flag: a threshold chosen per point is
# not a threshold.
REQUIRED_MARGIN = 2.0
THREAD_BUDGET_CORES = 1.0
ONE_THREAD_IS_THE_CAP = 0.8

EXIT_PASS = 0
EXIT_CLIENT_BOUND = 1
EXIT_REFUSED = 3

LABEL_BATCH_RE = re.compile(r"\bbatch=(\d+)")
LABEL_CONCURRENCY_RE = re.compile(r"\bconcurrency=(\d+)")
PHASE_0_OWES = ("owed from Phase 0 client calibration "
                "(BUILD-RATE-MATRIX-PLAN.md, 'Phase 0')")
UNMEASURED = "not measured — this clause cannot pass on absent evidence"


@dataclass(frozen=True)
class Peak:
    value: float
    at: int | None


@dataclass(frozen=True)
class Ceilings:
    engine: str
    measured_on: str
    ops_per_s: dict[str, Any]
    loader_core_bound_at: float | None


@dataclass(frozen=True)
class Reading:
    label: str
    box_peak: Peak | None
    box_median: float | None
    cores_available: int | None
    cores_source: str
    loader_peak: Peak | None
    thread_peak: Peak | None
    threads_peak: Peak | None
    steal_peak: Peak | None
    wait_median: float | None


@dataclass(frozen=True)
class Clause:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Gate:
    batch_size: int
    concurrency: int
    achieved_docs_per_s: float
    cores: int | None
    cores_source: str
    ceilings: Ceilings
    reading: Reading

    @property
    def achieved_ops_per_s(self) -> float:
        return self.achieved_docs_per_s / self.batch_size

    @property
    def ceiling_ops_per_s(self) -> float | None:
        return positive_float(self.ceilings.ops_per_s.get(str(self.batch_size)))

    def cpu_bound(self, budget_cores: float) -> float | None:
        if self.ceilings.loader_core_bound_at is None:
            return None
        return self.ceilings.loader_core_bound_at * budget_cores


def positive_float(value: Any) -> float | None:
    """A null or non-positive entry is a placeholder for a measurement nobody
    has taken, and must not be read as a threshold."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def cores_text(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}"


def load_ceilings(path: str) -> Ceilings | None:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    levels = document.get("ops_per_s")
    return Ceilings(
        engine=str(document.get("engine") or "unrecorded engine"),
        measured_on=str(document.get("measured_on") or ""),
        ops_per_s=dict(levels) if isinstance(levels, dict) else {},
        loader_core_bound_at=positive_float(document.get("loader_core_bound_at")),
    )


def peak_of(records: list[dict[str, Any]], field: str, tag: str) -> Peak | None:
    samples = [(record[field], record.get(tag)) for record in records
               if record.get(field) is not None]
    if not samples:
        return None
    value, at = max(samples, key=lambda sample: sample[0])
    return Peak(value=float(value), at=at if isinstance(at, int) else None)


def median_of(records: list[dict[str, Any]], field: str) -> float | None:
    values = [float(record[field]) for record in records
              if record.get(field) is not None]
    return statistics.median(values) if values else None


def cores_budget(box: list[dict[str, Any]]) -> tuple[int | None, str]:
    """The loader's own cpuset, not the probe's.

    `generator_probe.box_counters` falls back to the probe's affinity while no
    loader exists yet, and on a pinned run the two differ — judging a 4-core
    loader against an 8-core denominator would hide half its utilisation.
    """
    measured = [record for record in box
                if record.get("cores_source") == LOADER_CORES_SOURCE]
    latest = measured or box
    if not latest:
        return None, ""
    cores = latest[-1].get("cores_available")
    return (cores if isinstance(cores, int) else None,
            str(latest[-1].get("cores_source") or ""))


def read_series(path: str) -> Reading:
    header, records = runmeta.read_jsonl(path)
    box = [record for record in records if record.get("record") == BOX_RECORD]
    loaders = [record for record in records
               if record.get("record") == LOADER_RECORD]
    cores, source = cores_budget(box)
    return Reading(
        label=str(header.get("label") or ""),
        box_peak=peak_of(box, "cpu_cores_used", "i"),
        box_median=median_of(box, "cpu_cores_used"),
        cores_available=cores, cores_source=source,
        loader_peak=peak_of(loaders, "cpu_cores_used", "pid"),
        thread_peak=peak_of(loaders, "busiest_thread_cores", "pid"),
        threads_peak=peak_of(loaders, "threads", "pid"),
        steal_peak=peak_of(box, "steal_cores", "i"),
        wait_median=median_of(loaders, "runq_wait_ratio"),
    )


def measured_levels(ceilings: Ceilings) -> list[int]:
    """Only the levels carrying a positive figure. Naming a null entry as one
    the document "carries" would describe a placeholder as a measurement."""
    return sorted(int(level) for level, value in ceilings.ops_per_s.items()
                  if str(level).isdigit() and positive_float(value) is not None)


def ceiling_refusal(gate: Gate) -> str:
    if gate.ceiling_ops_per_s is not None:
        return ""
    known = ", ".join(str(level) for level in measured_levels(gate.ceilings))
    return (f"no measured operations/s ceiling for --batch-size "
            f"{gate.batch_size} in the ceilings document (it carries: "
            f"{known or 'none'}); that level's client ceiling is "
            f"{PHASE_0_OWES}, and no other level's figure substitutes for it")


def bound_refusal(gate: Gate) -> str:
    if gate.ceilings.loader_core_bound_at is not None:
        return ""
    return (f"the ceilings document carries no loader_core_bound_at; the CPU "
            f"threshold is {PHASE_0_OWES} and is not defaulted here — the 0.70 "
            f"in the plan is anchored to a pre-rewrite anecdote")


def token_refusal(label: str, pattern: re.Pattern[str], token: str, flag: str,
                  expected: int) -> str:
    """One half of "is this series about the point being judged?".

    Both halves are compared against a required flag rather than against the
    file name: a name is a label and the header is the record, which is the
    rule the batch axis keeps for its own per-level directories.
    """
    match = pattern.search(label)
    if not match or int(match.group(1)) == expected:
        return ""
    return (f"the series label says {token}={match.group(1)} but the gate was "
            f"given {flag} {expected}: this is another point's generator "
            f"series, so the verdict would be about that point")


def label_batch_refusal(gate: Gate) -> str:
    return token_refusal(gate.reading.label, LABEL_BATCH_RE, "batch",
                         "--batch-size", gate.batch_size)


def label_concurrency_refusal(gate: Gate) -> str:
    """A sweep runs a whole concurrency ladder at one batch level, so `batch=`
    agrees on every rung of it and only this token says which rung a series
    covers. Without it, a neighbouring rung's probe — the c=8 warm-up's, say —
    turned a point proven client-bound into a PASS."""
    return token_refusal(gate.reading.label, LABEL_CONCURRENCY_RE,
                         "concurrency", "--concurrency", gate.concurrency)


def label_slice_refusal(gate: Gate) -> str:
    """A probe run once over a whole multi-point pass carries a label that names
    no rung at all. Clearing on it would be a verdict about every point at once,
    which is a verdict about none of them."""
    if LABEL_CONCURRENCY_RE.search(gate.reading.label):
        return ""
    return (f"the series label {gate.reading.label!r} names no "
            f"concurrency=<n>, so nothing in it shows the series covers the "
            f"point being judged — a probe run over a whole pass measures "
            f"every point at once. Slice the generator series to this point's "
            f"window and label it as tools/sweep_build_rate.sh does "
            f"(concurrency=<n> batch=<n>)")


def cores_refusal(gate: Gate) -> str:
    if gate.cores and gate.cores > 0:
        return ""
    return ("the series recorded no cores_available for the loader's cpuset "
            "and --cores was not given, so the box CPU clause has no "
            "denominator")


def box_refusal(gate: Gate) -> str:
    if gate.reading.box_peak is not None:
        return ""
    return (f"no {BOX_RECORD} record carries a non-null cpu_cores_used — the "
            f"probe wrote fewer than two ticks, or it was not running over "
            f"this point's window")


def thread_refusal(gate: Gate) -> str:
    if gate.reading.thread_peak is not None:
        return ""
    return (f"no {LOADER_RECORD} record carries a non-null "
            f"busiest_thread_cores — no loader matched the probe's --match, so "
            f"the pinned-thread clause cannot be evaluated and box CPU alone "
            f"would pass the case this gate exists to catch")


def refusals(gate: Gate) -> list[str]:
    checks = (ceiling_refusal, bound_refusal, label_batch_refusal,
              label_concurrency_refusal, label_slice_refusal, cores_refusal,
              box_refusal, thread_refusal)
    return [problem for problem in (check(gate) for check in checks) if problem]


def unmeasured(name: str) -> Clause:
    return Clause(name=name, ok=False, detail=UNMEASURED)


def operation_clause(gate: Gate) -> Clause:
    ceiling = gate.ceiling_ops_per_s
    if ceiling is None:
        return unmeasured("operations/s")
    achieved = gate.achieved_ops_per_s
    margin = ceiling / achieved
    return Clause(
        name="operations/s", ok=margin >= REQUIRED_MARGIN,
        detail=(f"{achieved:,.1f} achieved ({gate.achieved_docs_per_s:,.1f} "
                f"docs/s / {gate.batch_size}) vs {ceiling:,.1f} ceiling — "
                f"{margin:.2f}x, need {REQUIRED_MARGIN:.2f}x"))


def box_detail(gate: Gate, peak: Peak, bound: float) -> str:
    return (f"{cores_text(peak.value)} cores peak at tick {peak.at}, median "
            f"{cores_text(gate.reading.box_median)} of {gate.cores} "
            f"({gate.cores_source}) — bound {bound:.2f}")


def box_clause(gate: Gate) -> Clause:
    peak, bound = gate.reading.box_peak, gate.cpu_bound(gate.cores or 0)
    if peak is None or not bound:
        return unmeasured("box CPU")
    return Clause(name="box CPU", ok=peak.value < bound,
                  detail=box_detail(gate, peak, bound))


def thread_clause(gate: Gate) -> Clause:
    peak, bound = gate.reading.thread_peak, gate.cpu_bound(THREAD_BUDGET_CORES)
    if peak is None or not bound:
        return unmeasured("busiest thread")
    return Clause(name="busiest thread", ok=peak.value < bound,
                  detail=(f"{cores_text(peak.value)} cores peak (pid "
                          f"{peak.at}) of one core — bound {bound:.2f}"))


def verdict_clauses(gate: Gate) -> list[Clause]:
    return [operation_clause(gate), box_clause(gate), thread_clause(gate)]


def gil_cause(share: float, process_cores: float,
              bound: float | None) -> str:
    """Below its own bound nothing is the cap, and the split then says nothing
    about what would bind first — a loader at 0.01 of a core is not "one thread
    is the cap" merely because that one thread holds all 0.01. The calibrated
    bound is reused rather than a second floor invented for a diagnosis line."""
    if bound is None or process_cores < bound:
        return "process well inside its budget, so neither is the cap yet"
    return ("one thread is the cap" if share >= ONE_THREAD_IS_THE_CAP
            else "GIL round-robin, so more processes are the fix")


def gil_note(reading: Reading, bound: float | None) -> str:
    """Straight from `generator_probe`'s docstring: busiest close to the process
    total means one thread is the cap, which is what a single event loop looks
    like; busiest well below it means the GIL is being round-robined and more
    *processes* are the fix."""
    process, thread = reading.loader_peak, reading.thread_peak
    if process is None or thread is None or process.value <= 0:
        return ""
    share = thread.value / process.value
    return (f"busiest thread {cores_text(thread.value)} of process peak "
            f"{cores_text(process.value)} cores ({share * 100:.0f}%) — "
            f"{gil_cause(share, process.value, bound)}")


def thread_count_note(reading: Reading) -> str:
    if reading.threads_peak is None:
        return ""
    return (f"peak threads in one loader process: "
            f"{reading.threads_peak.value:.0f}")


def steal_note(reading: Reading) -> str:
    if reading.steal_peak is None or reading.steal_peak.value <= 0:
        return ""
    return (f"box steal peaked at {cores_text(reading.steal_peak.value)} cores "
            f"— the box's own CPU reading understates by that much")


def wait_note(reading: Reading) -> str:
    """Median rather than peak, and the denominator is named: the ratio is over
    the loader's own run+wait time, which is a small denominator while the
    loader is mostly asleep, so a single tick of it is not starvation."""
    if reading.wait_median is None or reading.wait_median <= 0:
        return ""
    return (f"median runqueue wait {reading.wait_median * 100:.1f}% of the "
            f"loader's own run+wait time")


def diagnosis(gate: Gate) -> list[str]:
    """Not verdict inputs. `generator_probe` is explicit that the thread and
    runqueue figures are diagnosis, so they are printed rather than judged — a
    client-bound verdict then arrives with the shape of its cause attached."""
    reading = gate.reading
    notes = (gil_note(reading, gate.cpu_bound(THREAD_BUDGET_CORES)),
             thread_count_note(reading), steal_note(reading),
             wait_note(reading))
    return [note for note in notes if note]


def observed_line(clauses: list[Clause]) -> str:
    return "observed: " + "; ".join(f"{clause.name} {clause.detail}"
                                    for clause in clauses)


def print_report(gate: Gate, clauses: list[Clause]) -> None:
    print(f"generator gate: {gate.ceilings.engine} loader at --batch-size "
          f"{gate.batch_size}, client ceiling measured on "
          f"{gate.ceilings.measured_on or 'an unrecorded box'}")
    if gate.reading.label:
        print(f"  point: {gate.reading.label}")
    for clause in clauses:
        print(f"  {'clear' if clause.ok else 'BOUND'} {clause.name:<15} "
              f"{clause.detail}")
    for note in diagnosis(gate):
        print(f"  note: {note}")


def alarm(message: str) -> None:
    """stdout is block-buffered when a caller captures it, so without the flush
    the verdict lands ahead of the report it is a verdict on."""
    sys.stdout.flush()
    print(message, file=sys.stderr)


def refuse(problems: list[str]) -> int:
    for problem in problems:
        alarm(f"GATE REFUSED: {problem}")
    alarm("GATE REFUSED: no verdict — this is not a pass, and the point is not "
          "shown to be engine-bound")
    return EXIT_REFUSED


def report_verdict(gate: Gate, clauses: list[Clause]) -> int:
    print_report(gate, clauses)
    print(observed_line(clauses))
    failed = [clause.name for clause in clauses if not clause.ok]
    if not failed:
        print("GENERATOR GATE PASS: the point is not client-bound")
        return EXIT_PASS
    alarm(f"CLIENT-BOUND: {', '.join(failed)} — this point is a lower bound, "
          f"drawn hollow and never plotted as an engine number")
    return EXIT_CLIENT_BOUND


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cpu-series", required=True,
                        help="generator_probe JSONL over this point's window")
    parser.add_argument("--batch-size", type=int, required=True,
                        help="documents per operation, as the point ran it")
    parser.add_argument("--concurrency", type=int, required=True,
                        help="the rung this point sits on, as the point ran "
                             "it; checked against the series label so another "
                             "rung's probe cannot answer for it")
    parser.add_argument("--achieved-docs-per-s", type=float, required=True,
                        help="the point's own build rate")
    parser.add_argument("--ceilings", required=True,
                        help="Phase 0 client ceilings JSON for this loader")
    parser.add_argument("--cores", type=int, default=None,
                        help="override the core budget the series recorded")
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.achieved_docs_per_s <= 0:
        parser.error("--achieved-docs-per-s must be positive; a point with no "
                     "build rate has nothing to judge")
    return args


def build_gate(args: argparse.Namespace, ceilings: Ceilings,
               reading: Reading) -> Gate:
    cores = args.cores or reading.cores_available
    source = "--cores" if args.cores else reading.cores_source
    return Gate(batch_size=args.batch_size, concurrency=args.concurrency,
                achieved_docs_per_s=args.achieved_docs_per_s,
                cores=cores, cores_source=source or "unrecorded",
                ceilings=ceilings, reading=reading)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ceilings = load_ceilings(args.ceilings)
    if ceilings is None:
        return refuse([f"--ceilings {args.ceilings} is missing or unparseable; "
                       f"the client ceilings are {PHASE_0_OWES}"])
    try:
        reading = read_series(args.cpu_series)
    except OSError as err:
        return refuse([f"--cpu-series {args.cpu_series} is unreadable ({err}); "
                       f"the generator was not measured over this point"])
    gate = build_gate(args, ceilings, reading)
    problems = refusals(gate)
    if problems:
        return refuse(problems)
    return report_verdict(gate, verdict_clauses(gate))


if __name__ == "__main__":
    raise SystemExit(main())
