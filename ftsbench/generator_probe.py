"""What the load generator itself cost — the harness box, not the engine.

`resource_probe` samples the SUT's containers. Nothing has ever sampled the box
the loaders run on, so every conclusion of the form "the client was the
bottleneck" in this repo was inferred from the shape of a throughput curve.
`BUILD-RATE-LOOP.md` records the gap directly: verify_cpu_usage checks *engine*
saturation, and nothing was watching the generator.

That mattered. Measured 2026-09-08 (`results/client-model-2026-09-08/README.md`):
at `--concurrency 64` the loader burst to 35,000 documents in 2.2 s and then
settled to ~800 rows/s at 13% CPU with 77 threads asleep in futex; two threads
holding 1,000 outstanding CQL statements delivered 9,024 docs/s where 64 threads
holding 64 delivered 2,594. None of that is visible from the SUT.

**Why a separate process rather than a `--containers` entry.**
`tools/sut_probe.sh` runs `resource_probe` ON the SUT, because `/sys/fs/cgroup`
cannot cross `DOCKER_HOST=ssh://` — so that process's `/proc` is the SUT's, not
the harness's. It then `scp`s the remote series over the local `cpu-*.jsonl`,
which would destroy anything a local probe appended to the same file.

**Why a different record name.** `plotlib.load_runs` keeps only records whose
`record` field matches what the chart asked for, so a distinct name is free
protection against every site that sums across a file — `plot_growth.side_totals`
and `plot_c4`'s RSS and CPU totals among them. A generator record folded into
those would add client CPU to the engine's own, in the very charts used to tell
an engine ceiling from a client artifact. For the same reason no record here
carries a `role` key: `tools/inline_ab_report.probe_peaks` buckets any line that
has one, whatever its record type.

**What is the gate input and what is not.** `cpu_cores_used` per process is the
verdict. `busiest_thread_cores` and `threads` are diagnosis: busiest close to the
process total means one thread is the cap, which is what a single event loop
looks like; busiest well below it means the GIL is being round-robined and more
*processes* are the fix. Nulls stay null — a fabricated 0 reads as "no
starvation", which is a claim, and it is the same rule `resource_probe` follows
for its own first tick.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import runmeta

DEFAULT_INTERVAL_S = 1.0
DEFAULT_MATCHES = ("ftsbench.opensearch_load", "ftsbench.scylla_load",
                   "ftsbench.churn_load")
SLEEP_SLICE_S = 0.25
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NANOSECONDS = 1e9
MICROSECONDS = 1e6
UNMATCHED_WARN_TICKS = 30

# Every /proc read goes through this so the tests can point the probe at a
# fixture tree. The alternative — threading a root through nine signatures —
# would put a test-only parameter in the production call path.
PROC_ROOT = Path("/proc")

RSS_SOURCE_NOTE = ("/proc/<pid>/statm resident, INCLUDES shared pages — not the "
                   "cgroup anon figure resource_sample records")
RUNQUEUE_SOURCE_NOTE = ("/proc/<pid>/task/<tid>/schedstat fields 1 and 2; null "
                        "when CONFIG_SCHEDSTATS is off, never 0")
GIL_NOTE = ("BUILD-RATE-LOOP.md measured a loader PROVEN GIL-bound by A/B "
            "(2 disjoint shards ~= 2x aggregate) sitting at ~80% of one core; "
            "a threshold of 0.85 would have passed it")


@dataclass(frozen=True)
class ProcessCounters:
    pid: int
    cmd: str
    threads: int
    cpu_seconds: float
    busiest_thread_seconds: float
    run_seconds: float | None
    wait_seconds: float | None
    rss_bytes: int | None


@dataclass(frozen=True)
class BoxCounters:
    cores_available: int
    cores_source: str
    cpu_seconds: float
    steal_seconds: float
    pressure_seconds: float | None
    mem_available_bytes: int | None
    swap_used_bytes: int | None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def thread_cpu_seconds(task_dir: Path) -> float | None:
    """utime + stime for one thread.

    Parsed after the LAST `) ` rather than by splitting on whitespace: field 2
    is the executable name in parentheses and may itself contain spaces and
    parens, which would shift every later field and silently misreport CPU.
    """
    raw = _read(task_dir / "stat")
    if raw is None:
        return None
    tail = raw.rpartition(") ")[2].split()
    if len(tail) < 13:
        return None
    return (int(tail[11]) + int(tail[12])) / CLOCK_TICKS


def thread_sched_seconds(task_dir: Path) -> tuple[float, float] | None:
    """(on-CPU, runqueue-wait) for one thread, or None if unavailable.

    None rather than zeros when CONFIG_SCHEDSTATS is off: a zero wait reads as
    "this thread was never starved", and the gate must decline to evaluate that
    clause rather than assert it.
    """
    raw = _read(task_dir / "schedstat")
    if raw is None:
        return None
    fields = raw.split()
    if len(fields) < 2:
        return None
    return int(fields[0]) / NANOSECONDS, int(fields[1]) / NANOSECONDS


def process_argv(pid: int) -> list[str]:
    """argv as the kernel stored it, NUL-separated and unjoined.

    Unjoined on purpose. `make`'s recipe shell carries the whole python command
    line in its own argv, so a substring test on the joined string matches the
    `/bin/sh -c` wrapper as well as the loader — measured here as four
    "loaders" for one real one, which inflates `loaders_running`, widens the
    gate's window to cover the wrapper's lifetime, and lets a shell with no CPU
    of its own count as a generator process.
    """
    raw = _read(PROC_ROOT / str(pid) / "cmdline") or ""
    return [part for part in raw.split("\0") if part]


def process_command(pid: int) -> str:
    return " ".join(process_argv(pid))


def resident_bytes(pid: int) -> int | None:
    raw = _read(PROC_ROOT / str(pid) / "statm")
    if raw is None:
        return None
    fields = raw.split()
    return int(fields[1]) * PAGE_SIZE if len(fields) > 1 else None


def cpus_allowed(pid: int) -> tuple[int, ...] | None:
    """The loader's OWN core budget, from /proc/<pid>/status.

    Read from the loader rather than from this probe's affinity so the probe
    needs no `taskset` wrapper — pinning it inside the budget it is judging
    would make it part of the measurement — and so no Makefile variable has to
    agree with a shell one about what GEN_CPUSET is.
    """
    raw = _read(PROC_ROOT / str(pid) / "status")
    if raw is None:
        return None
    for line in raw.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return parse_cpu_list(line.split(":", 1)[1].strip())
    return None


def parse_cpu_list(text: str) -> tuple[int, ...]:
    """The Linux cpulist grammar: comma-separated singletons and `a-b` ranges."""
    cores: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            cores.extend(range(int(low), int(high) + 1))
        else:
            cores.append(int(part))
    return tuple(sorted(set(cores)))


def read_process(pid: int) -> ProcessCounters | None:
    """Sum this process's threads, keeping the per-thread maximum.

    A thread that exits between the directory listing and the read is normal
    under an event loop that adjusts its executor, so a vanished thread is
    skipped rather than aborting the tick and losing the whole sample.
    """
    task_root = PROC_ROOT / str(pid) / "task"
    try:
        task_dirs = list(task_root.iterdir())
    except OSError:
        return None

    cpu_total, busiest, threads = 0.0, 0.0, 0
    run_total, wait_total, sched_seen = 0.0, 0.0, False
    for task_dir in task_dirs:
        seconds = thread_cpu_seconds(task_dir)
        if seconds is None:
            continue
        threads += 1
        cpu_total += seconds
        busiest = max(busiest, seconds)
        sched = thread_sched_seconds(task_dir)
        if sched is not None:
            sched_seen = True
            run_total += sched[0]
            wait_total += sched[1]
    if not threads:
        return None
    return ProcessCounters(
        pid=pid, cmd=process_command(pid), threads=threads,
        cpu_seconds=cpu_total, busiest_thread_seconds=busiest,
        run_seconds=run_total if sched_seen else None,
        wait_seconds=wait_total if sched_seen else None,
        rss_bytes=resident_bytes(pid),
    )


class LoaderFinder:
    """Finds loader pids by command line, rescanning every tick.

    Rescanning rather than freezing a set at t=0 because the loader does not
    exist yet when the probe starts: the Makefile's c1_run starts the monitor
    and sleeps before launching the loader, and a sharded run starts several
    loaders a second apart. A frozen set would measure an empty box.

    Negative results are cached so a full scan is not repeated per tick for
    processes already known not to match, and the cache is dropped whenever a
    pid disappears so a recycled pid cannot inherit a stale verdict.
    """

    def __init__(self, matches: Iterable[str]) -> None:
        self._matches = tuple(matches)
        self._not_loaders: set[int] = set()

    def pids(self) -> list[int]:
        live, found = set(), []
        for entry in PROC_ROOT.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            live.add(pid)
            if pid in self._not_loaders:
                continue
            if self._is_loader(pid):
                found.append(pid)
            else:
                self._not_loaders.add(pid)
        self._not_loaders &= live
        return sorted(found)

    def _is_loader(self, pid: int) -> bool:
        """`python -m <module>` as adjacent argv tokens, not a substring.

        A shell that merely mentions the module in its own argv is not the
        loader, and counting it would put a process with no CPU of its own into
        a gate whose whole verdict is per-process CPU.
        """
        argv = process_argv(pid)
        return any(argv[i] == "-m" and argv[i + 1] in self._matches
                   for i in range(len(argv) - 1))


def read_box(cores: tuple[int, ...]) -> tuple[float, float]:
    """(busy, steal) CPU seconds summed over exactly `cores`.

    Per-core rows from /proc/stat rather than the cgroup root, because the root
    counts every core on the box — including, on the laptop, the engine cores
    the loader is deliberately kept away from. Restricting the numerator to the
    loader's own cpuset makes it the same machine as the denominator.
    """
    raw = _read(PROC_ROOT / "stat") or ""
    busy_total, steal_total = 0.0, 0.0
    wanted = {f"cpu{core}" for core in cores}
    for line in raw.splitlines():
        fields = line.split()
        if not fields or fields[0] not in wanted:
            continue
        values = [int(v) for v in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        busy_total += (sum(values) - idle) / CLOCK_TICKS
        if len(values) > 7:
            steal_total += values[7] / CLOCK_TICKS
    return busy_total, steal_total


def read_pressure() -> float | None:
    """Cumulative `some` CPU pressure in seconds; None when PSI is unavailable."""
    raw = _read(PROC_ROOT / "pressure" / "cpu")
    if raw is None:
        return None
    for line in raw.splitlines():
        if line.startswith("some"):
            for field in line.split():
                if field.startswith("total="):
                    return int(field.split("=")[1]) / MICROSECONDS
    return None


def read_meminfo() -> tuple[int | None, int | None]:
    raw = _read(PROC_ROOT / "meminfo") or ""
    values: dict[str, int] = {}
    for line in raw.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * 1024
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    swap_used = None if swap_total is None or swap_free is None else swap_total - swap_free
    return values.get("MemAvailable"), swap_used


class RateTracker:
    """Counter deltas as a rate, null until there is a predecessor.

    Reimplemented here rather than imported so `resource_probe` — the producer
    of every published C4/S14/S15 number — is not edited for this feature. It
    follows the same rule that module states for its own first tick: reporting
    0.0 there would draw something that was saturated at start-up as idle.
    """

    def __init__(self) -> None:
        self._previous: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, total: float | None, now: float) -> float | None:
        if total is None:
            self._previous.pop(key, None)
            return None
        previous = self._previous.get(key)
        self._previous[key] = (now, total)
        if previous is None or now <= previous[0]:
            return None
        return round((total - previous[1]) / (now - previous[0]), 4)

    def forget_missing(self, keys: set[str]) -> None:
        """A restarted loader gets a new pid, so its rate starts null again
        rather than differencing against a process that no longer exists."""
        for stale in set(self._previous) - keys:
            self._previous.pop(stale, None)


@dataclass(frozen=True)
class Tick:
    i: int
    elapsed_s: float
    unix_s: float


def loader_record(tick: Tick, counters: ProcessCounters,
                  rates: RateTracker) -> dict[str, Any]:
    key = str(counters.pid)
    wait_ratio = None
    run_rate = rates.rate(f"{key}:run", counters.run_seconds, tick.elapsed_s)
    wait_rate = rates.rate(f"{key}:wait", counters.wait_seconds, tick.elapsed_s)
    if run_rate is not None and wait_rate is not None and run_rate + wait_rate > 0:
        wait_ratio = round(wait_rate / (run_rate + wait_rate), 4)
    return {
        "record": "generator_sample",
        "i": tick.i,
        "t_elapsed_s": round(tick.elapsed_s, 3),
        "t_unix_s": round(tick.unix_s, 3),
        "pid": counters.pid,
        "cmd": counters.cmd[:200],
        "running": True,
        "source": "proc",
        "threads": counters.threads,
        "cpu_seconds_total": round(counters.cpu_seconds, 4),
        "cpu_cores_used": rates.rate(key, counters.cpu_seconds, tick.elapsed_s),
        "busiest_thread_cores": rates.rate(
            f"{key}:busiest", counters.busiest_thread_seconds, tick.elapsed_s),
        "run_seconds_total": counters.run_seconds,
        "runq_wait_seconds_total": counters.wait_seconds,
        "runq_wait_ratio": wait_ratio,
        "rss_bytes": counters.rss_bytes,
    }


def departed_record(tick: Tick, pid: int) -> dict[str, Any]:
    """A loader that exited mid-point still gets a tick.

    Same rule resource_probe applies to a container that stopped: the tick
    appears with nulls, so a generator that died is visible rather than looking
    like a window in which nothing was offered.
    """
    return {
        "record": "generator_sample", "i": tick.i,
        "t_elapsed_s": round(tick.elapsed_s, 3), "t_unix_s": round(tick.unix_s, 3),
        "pid": pid, "cmd": "", "running": False, "source": "proc",
        "threads": None, "cpu_seconds_total": None, "cpu_cores_used": None,
        "busiest_thread_cores": None, "run_seconds_total": None,
        "runq_wait_seconds_total": None, "runq_wait_ratio": None,
        "rss_bytes": None,
    }


def box_record(tick: Tick, box: BoxCounters, rates: RateTracker,
               loaders_running: int) -> dict[str, Any]:
    pressure_rate = rates.rate("box:pressure", box.pressure_seconds, tick.elapsed_s)
    return {
        "record": "generator_box_sample",
        "i": tick.i,
        "t_elapsed_s": round(tick.elapsed_s, 3),
        "t_unix_s": round(tick.unix_s, 3),
        "source": "proc",
        "cores_available": box.cores_available,
        "cores_source": box.cores_source,
        "cpu_seconds_total": round(box.cpu_seconds, 4),
        "cpu_cores_used": rates.rate("box", box.cpu_seconds, tick.elapsed_s),
        "steal_seconds_total": round(box.steal_seconds, 4),
        "steal_cores": rates.rate("box:steal", box.steal_seconds, tick.elapsed_s),
        "cpu_pressure_some_seconds_total": box.pressure_seconds,
        "cpu_pressure_some_ratio": pressure_rate,
        "mem_available_bytes": box.mem_available_bytes,
        "swap_used_bytes": box.swap_used_bytes,
        "loaders_running": loaders_running,
    }


def box_counters(loader_pids: list[int]) -> BoxCounters:
    """The box, measured over the loader's own cpuset.

    Falls back to this probe's affinity only while no loader exists yet; the
    fallback is recorded in `cores_source` rather than left to be inferred,
    because the two can differ on a taskset'd laptop run.
    """
    cores, source = None, "loader-cpus-allowed"
    for pid in loader_pids:
        cores = cpus_allowed(pid)
        if cores:
            break
    if not cores:
        cores, source = tuple(sorted(os.sched_getaffinity(0))), "probe-affinity"
    busy, steal = read_box(cores)
    available, swap = read_meminfo()
    return BoxCounters(cores_available=len(cores), cores_source=source,
                       cpu_seconds=busy, steal_seconds=steal,
                       pressure_seconds=read_pressure(),
                       mem_available_bytes=available, swap_used_bytes=swap)


def print_tick(tick: Tick, loaders: list[dict[str, Any]],
               box: dict[str, Any]) -> None:
    """Visible during a multi-hour sweep, not only in the closing gate."""
    for record in loaders:
        cores = record["cpu_cores_used"]
        busiest = record["busiest_thread_cores"]
        rss = record["rss_bytes"]
        print(f"t={tick.elapsed_s:6.1f}s loader pid={record['pid']}: "
              f"cores={'--' if cores is None else f'{cores:.2f}'} "
              f"busiest={'--' if busiest is None else f'{busiest:.2f}'} "
              f"thr={record['threads']} "
              f"rss={'--' if rss is None else f'{rss / 2**20:.0f}MiB'}",
              file=sys.stderr)
    used = box["cpu_cores_used"]
    print(f"t={tick.elapsed_s:6.1f}s box "
          f"{'--' if used is None else f'{used:.2f}'}/{box['cores_available']} "
          f"loaders={box['loaders_running']}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="JSONL time series path")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="0 = until terminated")
    parser.add_argument("--engine", default="",
                        help="recorded only; this artifact measures the harness")
    parser.add_argument("--label", default="")
    parser.add_argument("--cache-state", default="unspecified")
    parser.add_argument("--corpus", default="")
    parser.add_argument("--match", action="append", default=None,
                        help="substring of a loader's command line; repeatable")
    return parser.parse_args(argv)


def build_header(args: argparse.Namespace) -> dict[str, Any]:
    return runmeta.header(
        producer="generator_probe", engine=args.engine or "n/a",
        engine_version="n/a — this artifact measures the harness box",
        label=args.label, cache_state=args.cache_state, corpus=args.corpus,
        max_docs=0, interval_s=args.interval, duration_s=args.duration,
        matches=list(args.match or DEFAULT_MATCHES),
        cpu_source="/proc/<pid>/task/<tid>/stat utime+stime",
        rss_source=RSS_SOURCE_NOTE,
        runqueue_source=RUNQUEUE_SOURCE_NOTE,
        pressure_source="/proc/pressure/cpu 'some total='; null when PSI is off",
        gil_note=GIL_NOTE,
    )


def expired(duration_s: float, started_s: float) -> bool:
    return bool(duration_s) and time.perf_counter() - started_s >= duration_s


def sleep_slices(interval_s: float, stopper: runmeta.Stopper) -> None:
    """Sliced so a SIGTERM lands at a record boundary rather than mid-write."""
    remaining = interval_s
    while remaining > 0 and not stopper.stop:
        time.sleep(min(SLEEP_SLICE_S, remaining))
        remaining -= SLEEP_SLICE_S


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    finder = LoaderFinder(args.match or DEFAULT_MATCHES)
    rates = RateTracker()
    stopper = runmeta.Stopper()
    started_s = time.perf_counter()
    seen_loader = False

    with open(args.output, "w", encoding="utf-8") as stream:
        runmeta.write_record(stream, build_header(args))
        index = 0
        while not stopper.stop and not expired(args.duration, started_s):
            tick = Tick(index, time.perf_counter() - started_s, time.time())
            pids = finder.pids()
            counters = [read_process(pid) for pid in pids]
            records = [loader_record(tick, c, rates) for c in counters if c]
            records += [departed_record(tick, pid)
                        for pid, c in zip(pids, counters) if c is None]
            rates.forget_missing({str(c.pid) for c in counters if c}
                                 | {f"{c.pid}:{k}" for c in counters if c
                                    for k in ("run", "wait", "busiest")}
                                 | {"box", "box:steal", "box:pressure"})
            box = box_record(tick, box_counters(pids), rates,
                             sum(1 for c in counters if c))
            for record in records + [box]:
                runmeta.write_record(stream, record)
            print_tick(tick, records, box)
            seen_loader = seen_loader or bool(records)
            if not seen_loader and index == UNMATCHED_WARN_TICKS:
                print(f"WARNING: no loader matched {args.match or DEFAULT_MATCHES} "
                      f"after {UNMATCHED_WARN_TICKS} ticks — the gate will have "
                      "only box CPU to judge on", file=sys.stderr)
            index += 1
            sleep_slices(args.interval, stopper)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
