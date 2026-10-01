"""CPU and memory on the build chart's own x axis: documents in the index.

`probe_windows` reduces a rung to one row — peak and median CPU, peak RSS. That
is the right shape for a bar per concurrency level and the wrong one for the
question `build-rate/charts/rate_vs_index_size.py` asks, where a single build
runs for a quarter of an hour and the whole point is what changes *while* it
runs. A peak over 8.9 million documents says nothing about whether the cost of
indexing the eight-millionth document differs from the cost of the first.

So this joins the two series the harness already writes, per tick:

- the probe's `resource_sample` records — `cpu_cores_used`, `rss_bytes`,
  `cache_bytes`, placed on a wall clock as `header.started_at + t_elapsed_s`,
  exactly as `probe_windows` places them;
- the per-second series `core/src/samples.rs` writes — `docs_indexed` and
  `index_docs_per_s`, whose `t_s` is relative to the build start that the
  epoch-stamped stderr tape supplies.

Both timelines are reconstructed by `probe_windows`, which is imported rather
than restated: the window boundaries, the stderr scan and the epoch placement
are its definitions and must not fork.

**The probe drives the rows.** One row per probe tick per container, carrying
the index size that tick was measured at. Driving off the sample series instead
would interpolate CPU, which is a rate over a tick and cannot be resampled
without inventing it.

**A tick whose nearest reading is further away than `--max-skew` is dropped and
counted**, never joined to whatever was closest. The two series are polled by
different processes on different machines; a gap means one of them stalled, and
a row pairing a CPU spike with an index size from four seconds later would read
as a cost that the index size never caused.

**Two rate columns, because they answer different questions.**
`index_docs_per_s` is the harness's own number, copied from the row that was
joined, and means everywhere what it means in `core/src/samples.rs`.
`tick_docs_per_s` is recomputed here over the gap between consecutive *joined*
readings. Where the probe ticks more slowly than the index is polled — the
usual case — the copied column is one poll's worth of a count that only
advances at a commit, so it reads 0 on most ticks and carries a whole
commit's work on the few in between. The recomputed one divides the documents
by the time they actually took at this file's resolution, which is the rate to
read against a CPU number sampled at that same resolution.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import probe_windows as windows
from . import runmeta

COLUMNS = [
    "arm", "sweep", "rep", "concurrency", "container", "role", "t_s",
    "docs_indexed", "index_docs_per_s", "tick_docs_per_s", "docs_submitted",
    "submit_docs_per_s", "cpu_cores_used", "rss_bytes", "cache_bytes",
    "shmem_bytes", "mem_limit_bytes", "index_size_bytes", "skew_s",
]
DEFAULT_MAX_SKEW_S = 1.0
CARRIED_FIELDS = ["cpu_cores_used", "rss_bytes", "cache_bytes", "shmem_bytes",
                  "mem_limit_bytes", "index_size_bytes"]


@dataclass(frozen=True)
class Reading:
    """One row of the per-second series, placed on the wall clock."""
    at: float
    t_s: float
    docs_indexed: int | None
    index_docs_per_s: float | None
    docs_submitted: int | None
    submit_docs_per_s: float | None


def number(raw: str | None, cast: type) -> Any:
    """Blank cells are the harness's "not watched", never a zero."""
    if raw is None or raw == "":
        return None
    try:
        return cast(raw)
    except ValueError:
        return None


def data_rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as handle:
        body = (line for line in handle if not line.startswith("#"))
        return list(csv.DictReader(body))


def reading_of(row: dict[str, str], build_start: float) -> Reading:
    t_s = float(row["t_s"])
    return Reading(
        at=build_start + t_s, t_s=t_s,
        docs_indexed=number(row.get("docs_indexed"), int),
        index_docs_per_s=number(row.get("index_docs_per_s"), float),
        docs_submitted=number(row.get("docs_submitted"), int),
        submit_docs_per_s=number(row.get("submit_docs_per_s"), float),
    )


def read_series(path: Path, build_start: float) -> list[Reading]:
    series = [reading_of(row, build_start) for row in data_rows(path)]
    return sorted(series, key=lambda reading: reading.at)


def samples_path_for(root: Path, window: windows.Window) -> Path:
    """`run-arm.sh`'s layout: one directory per rep, one file per level.

    Ambiguity is an error rather than a guess — two files for one rung means a
    repeated level, and pairing the wrong one with this window would put the
    CPU of one build against the index size of another.
    """
    pattern = str(root / f"{window.sweep}-rep{window.rep}"
                  / f"c{window.concurrency}-*.csv")
    found = sorted(glob.glob(pattern))
    if len(found) != 1:
        raise ValueError(f"expected exactly one series at {pattern}, "
                         f"found {len(found)}")
    return Path(found[0])


def nearest(series: Sequence[Reading], at: float) -> tuple[Reading, float]:
    times = [reading.at for reading in series]
    after = bisect.bisect_left(times, at)
    candidates = [index for index in (after - 1, after)
                  if 0 <= index < len(series)]
    best = min(candidates, key=lambda index: abs(series[index].at - at))
    return series[best], series[best].at - at


def row_for(arm: str, window: windows.Window, sample: windows.Timed,
            reading: Reading, skew: float) -> dict[str, Any]:
    record = sample.record
    carried = {field: record.get(field) for field in CARRIED_FIELDS}
    return {
        "arm": arm, "sweep": window.sweep, "rep": window.rep,
        "concurrency": window.concurrency,
        "container": record.get("container"), "role": record.get("role"),
        "t_s": round(reading.t_s, 3),
        "docs_indexed": reading.docs_indexed,
        "index_docs_per_s": reading.index_docs_per_s,
        "tick_docs_per_s": None,
        "docs_submitted": reading.docs_submitted,
        "submit_docs_per_s": reading.submit_docs_per_s,
        "skew_s": round(skew, 3), **carried,
    }


def tick_rate(previous: dict[str, Any], current: dict[str, Any]) -> float | None:
    """`None` rather than zero where it cannot be derived, so a missing reading
    never draws as an index that stopped."""
    gap = current["t_s"] - previous["t_s"]
    if gap <= 0 or previous["docs_indexed"] is None \
            or current["docs_indexed"] is None:
        return None
    return round((current["docs_indexed"] - previous["docs_indexed"]) / gap, 1)


def fill_tick_rates(rows: Sequence[dict[str, Any]]) -> None:
    """Per container: each one has its own tick series and its own first row,
    which has no predecessor to difference against."""
    seen: dict[Any, dict[str, Any]] = {}
    for row in rows:
        previous = seen.get(row["container"])
        if previous is not None:
            row["tick_docs_per_s"] = tick_rate(previous, row)
        seen[row["container"]] = row


def rows_for_window(arm: str, window: windows.Window,
                    sliced: Sequence[windows.Timed],
                    series: Sequence[Reading],
                    max_skew: float) -> tuple[list[dict[str, Any]], int]:
    rows, dropped = [], 0
    for sample in sliced:
        reading, skew = nearest(series, sample.at)
        if abs(skew) > max_skew:
            dropped += 1
            continue
        rows.append(row_for(arm, window, sample, reading, skew))
    fill_tick_rates(rows)
    return rows, dropped


def write_table(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def report(window: windows.Window, kept: int, dropped: int) -> None:
    print(f"{window.sweep} rep{window.rep} c{window.concurrency}: "
          f"{kept} rows, {dropped} ticks dropped past --max-skew",
          file=sys.stderr)


def collect(args: argparse.Namespace, samples: Sequence[windows.Timed],
            found: Sequence[windows.Window]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in found:
        series = read_series(samples_path_for(Path(args.samples_root), window),
                             window.build_start)
        if not series:
            raise ValueError(f"empty series for {window.key}")
        sliced = windows.samples_in(samples, window)
        produced, dropped = rows_for_window(args.arm, window, sliced, series,
                                            args.max_skew)
        report(window, len(produced), dropped)
        rows.extend(produced)
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--probe", required=True,
                        help="the arm's resource_probe JSONL")
    parser.add_argument("--stderr", required=True, action="append",
                        help="glob of run-arm.sh stderr tapes; repeatable")
    parser.add_argument("--samples-root", required=True,
                        help="directory holding one <sweep>-rep<N>/ per rep")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-skew", type=float, default=DEFAULT_MAX_SKEW_S,
                        help="seconds a probe tick may sit from its nearest "
                             f"index reading (default {DEFAULT_MAX_SKEW_S})")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    header, records = runmeta.read_jsonl(args.probe)
    samples = windows.timed_samples(header, records)
    found = windows.collect_windows(args.stderr)
    rows = collect(args, samples, found)
    if not rows:
        print("no rows joined; check --max-skew and the probe span",
              file=sys.stderr)
        return 1
    write_table(Path(args.out), rows)
    print(f"wrote {len(rows)} rows -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
