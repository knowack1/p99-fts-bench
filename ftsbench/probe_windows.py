"""Cut one arm's `resource_probe` series into per-concurrency-rung windows.

`INDEX-RATE-MATRIX-PLAN.md` runs **one** probe per arm, spanning a whole ladder
x N reps, and needs CPU and RSS attributed to each rung on the chart's x axis.
Nothing in the harness records a wall clock: the point CSV carries `wall_s`, the
per-second series carries `t_s`, and both are relative (`core/src/report.rs`,
`core/src/samples.rs`). The probe is the same — `resource_sample` carries only
`i` and `t_elapsed_s`, with `started_at` on the header record. So the join is
`header.started_at + t_elapsed_s` against the epoch-stamped stderr log that
`run-arm.sh` writes, which is the reconstruction `ftsbench/plot_growth.py`
already performs for the deck's growth charts.

Four decisions this module exists to get right:

- **The window is the build, not the level.** `run_sweep` announces a level
  *before* it opens the inserter (`core/src/sweep.rs`), so the span from
  `[i/N] concurrency=X` to `-> N docs in …s` includes the per-level keyspace
  drop and rebuild — on the ScyllaDB arms that is a DDL round trip plus a wait
  for the vector-store to forget the index, which is idle time that would drag
  a median CPU down and can carry a reclaim spike into a peak. The measured
  window therefore runs from `index is SERVING at 0 documents`
  (`scylla/src/reset.rs`) or `index is answering at 0 documents`
  (`opensearch/src/reset.rs`) to the result line. The reset span is kept as
  `reset_s` rather than dropped: a reset that grew across an arm is a finding.

- **One probe per arm gives every rung a usable first sample.** `cpu_cores_used`
  is a rate differenced against the previous tick and is `null` on the first
  tick *of the file* (`resource_probe.py`). Slicing an arm-wide series means
  only the arm's very first tick is lost, where a probe started and stopped per
  point throws one away at every rung.

- **There is no one memory number for the campaign's arms**, so `--memory-read`
  is required rather than defaulted. `rss_bytes` is cgroup v2 `memory.stat`
  *anon*: right for the ScyllaDB arms whose Tantivy index is in the
  vector-store's heap, but on the OpenSearch ramindex arms the index is tmpfs,
  which is `shmem` and not anon, and on the disk-backed arms it is `file`. A
  single default would silently report a 12 GiB index as free on the arms it
  did not suit.

- **Slices are named for the sweep, not the campaign arm.** The grid's two
  sub-sweeps overlap at `c=32` and both number their reps from 1, so
  `cpu-r2-c32-1.jsonl` would be written twice and one of the two rungs would be
  lost. The sweep name (`r2-low`, `r2-high`) is what makes the pair distinct,
  and it still satisfies `verify_cpu_usage.PROBE_RE`, which reads everything
  before `-c<conc>-<rep>` as the configuration.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import runmeta

CGROUP_SOURCE = "cgroup-anon"
MIN_SAMPLES = 5
SAMPLE_RECORD = "resource_sample"

LEVEL_RE = re.compile(r"\[\d+/\d+\] concurrency=(\d+)")
BUILD_START_RE = re.compile(r"index is (?:SERVING|answering) at 0 documents")
BUILD_END_RE = re.compile(r"->\s+\d+ docs in ")
REP_RE = re.compile(r"^(?P<sweep>.+)-rep(?P<rep>\d+)\.stderr\.tsv$")

MEMORY_FIELDS = {
    "anon": ("rss_bytes",),
    "anon+shmem": ("rss_bytes", "shmem_bytes"),
    "anon+cache": ("rss_bytes", "cache_bytes"),
}

TABLE_COLUMNS = [
    "arm", "sweep", "rep", "concurrency", "container", "role", "source",
    "samples", "reset_s", "build_s", "cpu_cores_peak", "cpu_cores_median",
    "rss_peak_bytes", "shmem_peak_bytes", "cache_peak_bytes",
    "mem_limit_bytes", "mem_peak_bytes", "mem_headroom_bytes",
    "index_docs_last", "note",
]


@dataclass(frozen=True)
class Window:
    sweep: str
    rep: int
    concurrency: int
    level_start: float
    build_start: float
    build_end: float

    @property
    def reset_s(self) -> float:
        return self.build_start - self.level_start

    @property
    def build_s(self) -> float:
        return self.build_end - self.build_start

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.sweep, self.concurrency, self.rep)

    @property
    def slice_name(self) -> str:
        return f"cpu-{self.sweep}-c{self.concurrency}-{self.rep}.jsonl"


@dataclass(frozen=True)
class Timed:
    at: float
    record: dict[str, Any]


def started_epoch(header: dict[str, Any]) -> float:
    started_at = header.get("started_at")
    if not started_at:
        raise ValueError("probe header carries no started_at; cannot place "
                         "t_elapsed_s on a wall clock")
    return datetime.fromisoformat(started_at).timestamp()


def timed_samples(header: dict[str, Any],
                  records: Sequence[dict[str, Any]]) -> list[Timed]:
    base = started_epoch(header)
    return [Timed(base + record["t_elapsed_s"], record) for record in records
            if record.get("record") == SAMPLE_RECORD
            and record.get("t_elapsed_s") is not None]


def sweep_and_rep(path: str) -> tuple[str, int]:
    match = REP_RE.match(Path(path).name)
    if match is None:
        raise ValueError(f"not a run-arm.sh stderr log: {path}")
    return match.group("sweep"), int(match.group("rep"))


def stamped_lines(path: str) -> Iterator[tuple[float, str]]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stamp, _, text = line.rstrip("\n").partition("\t")
            try:
                yield float(stamp), text
            except ValueError:
                continue


def scan_windows(lines: Iterable[tuple[float, str]], sweep: str,
                 rep: int) -> Iterator[Window]:
    concurrency: int | None = None
    level_start = build_start = 0.0
    for at, text in lines:
        level = LEVEL_RE.search(text)
        if level is not None:
            concurrency, level_start, build_start = int(level.group(1)), at, 0.0
        elif BUILD_START_RE.search(text):
            build_start = at
        elif BUILD_END_RE.search(text) and concurrency is not None:
            # A sweep run with --no-reset announces no SERVING line at all, and
            # there the level start IS the build start.
            yield Window(sweep, rep, concurrency, level_start,
                         build_start or level_start, at)
            concurrency = None


def windows_of(path: str) -> list[Window]:
    sweep, rep = sweep_and_rep(path)
    return list(scan_windows(stamped_lines(path), sweep, rep))


def collect_windows(patterns: Sequence[str]) -> list[Window]:
    paths = sorted({path for pattern in patterns
                    for path in glob.glob(pattern)})
    if not paths:
        raise ValueError(f"no stderr logs matched {list(patterns)}")
    return [window for path in paths for window in windows_of(path)]


def duplicate_keys(windows: Sequence[Window]) -> list[tuple[str, int, int]]:
    counts = collections.Counter(window.key for window in windows)
    return sorted(key for key, count in counts.items() if count > 1)


def samples_in(samples: Sequence[Timed], window: Window) -> list[Timed]:
    return [sample for sample in samples
            if window.build_start <= sample.at <= window.build_end]


def by_container(samples: Sequence[Timed]) -> dict[str, list[Timed]]:
    grouped: dict[str, list[Timed]] = {}
    for sample in samples:
        grouped.setdefault(sample.record.get("container", ""), []).append(sample)
    return grouped


def values_of(samples: Sequence[Timed], field: str) -> list[Any]:
    return [sample.record[field] for sample in samples
            if sample.record.get(field) is not None]


def peak(values: Sequence[Any]) -> Any:
    return max(values) if values else None


def median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def memory_of(record: dict[str, Any], fields: Sequence[str]) -> int | None:
    parts = [record.get(field) for field in fields]
    return None if any(part is None for part in parts) else sum(parts)


def memory_peak(samples: Sequence[Timed], fields: Sequence[str]) -> int | None:
    return peak([value for value in
                 (memory_of(sample.record, fields) for sample in samples)
                 if value is not None])


def headroom(limit: int | None, used: int | None) -> int | None:
    return None if limit is None or used is None else limit - used


def last_value(samples: Sequence[Timed], field: str) -> Any:
    values = values_of(samples, field)
    return values[-1] if values else None


def note_for(samples: Sequence[Timed]) -> str:
    if not samples:
        return "empty"
    return "thin" if len(samples) < MIN_SAMPLES else ""


def sources_of(samples: Sequence[Timed]) -> set[str]:
    return {str(sample.record.get("source")) for sample in samples}


def build_row(arm: str, window: Window, container: str,
              samples: Sequence[Timed], fields: Sequence[str]) -> dict[str, Any]:
    limit = peak(values_of(samples, "mem_limit_bytes"))
    used = memory_peak(samples, fields)
    return {
        "arm": arm,
        "sweep": window.sweep,
        "rep": window.rep,
        "concurrency": window.concurrency,
        "container": container,
        "role": last_value(samples, "role"),
        "source": "|".join(sorted(sources_of(samples))),
        "samples": len(samples),
        "reset_s": round(window.reset_s, 3),
        "build_s": round(window.build_s, 3),
        "cpu_cores_peak": peak(values_of(samples, "cpu_cores_used")),
        "cpu_cores_median": median(values_of(samples, "cpu_cores_used")),
        "rss_peak_bytes": peak(values_of(samples, "rss_bytes")),
        "shmem_peak_bytes": peak(values_of(samples, "shmem_bytes")),
        "cache_peak_bytes": peak(values_of(samples, "cache_bytes")),
        "mem_limit_bytes": limit,
        "mem_peak_bytes": used,
        "mem_headroom_bytes": headroom(limit, used),
        "index_docs_last": last_value(samples, "index_docs"),
        "note": note_for(samples),
    }


def rows_for(arm: str, window: Window, sliced: Sequence[Timed],
             fields: Sequence[str]) -> list[dict[str, Any]]:
    grouped = by_container(sliced)
    if not grouped:
        return [build_row(arm, window, "", [], fields)]
    return [build_row(arm, window, container, samples, fields)
            for container, samples in sorted(grouped.items())]


def write_slice(path: Path, header: dict[str, Any],
                sliced: Sequence[Timed]) -> None:
    with open(path, "w", encoding="utf-8") as out:
        runmeta.write_record(out, header)
        for sample in sliced:
            runmeta.write_record(out, sample.record)


def write_table(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=TABLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def bad_sources(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["samples"]
            and row["source"] != CGROUP_SOURCE]


def uncovered(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["note"] in ("empty", "thin")]


def describe(row: dict[str, Any]) -> str:
    return (f"{row['sweep']} c={row['concurrency']} rep={row['rep']} "
            f"{row['container'] or '(no samples)'}")


def report_problems(rows: Sequence[dict[str, Any]]) -> int:
    for row in uncovered(rows):
        print(f"  {row['note']}: {describe(row)} — {row['samples']} samples",
              file=sys.stderr)
    refused = bad_sources(rows)
    for row in refused:
        print(f"  REFUSED: {describe(row)} read from {row['source']}, not "
              f"{CGROUP_SOURCE}", file=sys.stderr)
    return 1 if refused else 0


def emit(args: argparse.Namespace, header: dict[str, Any],
         samples: Sequence[Timed],
         windows: Sequence[Window]) -> list[dict[str, Any]]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = MEMORY_FIELDS[args.memory_read]
    rows: list[dict[str, Any]] = []
    for window in windows:
        sliced = samples_in(samples, window)
        write_slice(out_dir / window.slice_name, header, sliced)
        rows.extend(rows_for(args.arm, window, sliced, fields))
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, help="campaign arm, e.g. r2")
    parser.add_argument("--probe", required=True,
                        help="the arm-wide resource_probe JSONL")
    parser.add_argument("--stderr", required=True, action="append",
                        help="glob for run-arm.sh stderr TSVs; repeatable")
    parser.add_argument("--out-dir", required=True,
                        help="where the per-rung slices are written")
    parser.add_argument("--table", required=True, help="summary CSV")
    parser.add_argument("--memory-read", required=True,
                        choices=sorted(MEMORY_FIELDS),
                        help="anon for the ScyllaDB arms, anon+shmem for a "
                             "tmpfs index, anon+cache for a file-backed one")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    header, records = runmeta.read_jsonl(args.probe)
    samples = timed_samples(header, records)
    windows = collect_windows(args.stderr)
    duplicates = duplicate_keys(windows)
    if duplicates:
        print(f"duplicate (sweep, concurrency, rep): {duplicates}",
              file=sys.stderr)
        return 1
    rows = emit(args, header, samples, windows)
    write_table(Path(args.table), rows)
    print(f"{args.arm}: {len(windows)} windows, {len(rows)} rows -> "
          f"{args.table}", file=sys.stderr)
    return report_problems(rows)


if __name__ == "__main__":
    raise SystemExit(main())
