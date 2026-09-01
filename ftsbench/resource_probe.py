"""Container resource footprint sampler — the grouped bars behind chart C4.

Samples every container in the stack on a fixed wall-clock tick and writes one
`resource_sample` record per container per tick (see SCHEMAS.md). Runs alongside
an ingest or query run rather than as a run of its own:

    python3 -m ftsbench.resource_probe --engine scylladb \\
        --containers fts-bench-scylla:scylladb \\
        --containers fts-bench-vector-store:vector-store \\
        --output data/c4-scylla-cdc-1.jsonl --interval 1 --duration 300 &

Five measurement decisions this module exists to get right:

- **The ScyllaDB side is two processes and C4 shows both.** ScyllaDB FTS runs
  the ScyllaDB cluster *plus* a separate vector-store cluster holding the
  in-RAM Tantivy index. One record per container with an explicit `role` lets
  the chart present that cost split and summed; a single merged number would
  hide where the memory actually goes, and the honest differentiator is that
  the sync pipeline is built into the product, not that there is one cluster.
  A `--engine scylladb` run that names no vector-store container is refused.

- **`rss_bytes` is the cgroup v2 `anon` figure from `memory.stat`, never
  `memory.current`.** `memory.current` includes page cache, so it would flatter
  whichever engine touched less disk — on the host this was written against the
  two differ by ~700 MB on one container. `cache_bytes` carries `file`
  separately for anyone who wants the total back.

- **`cpu_cores_used` is `null` on the first tick.** It is a rate derived from
  the monotonic `cpu.stat` `usage_usec` counter, and the first sample has no
  predecessor to difference against. Reporting 0.0 there would draw a container
  that was saturated at start-up as idle.

- **`index_size_bytes` is `null`, never 0, for the in-RAM Tantivy index.**
  OpenSearch reports `store.size_in_bytes`; Tantivy has no on-disk index to
  measure, and its cost appears in the vector-store's `rss_bytes` instead. A
  zero there would read as "ScyllaDB's index is free", which is false and is
  exactly the kind of claim this benchmark exists to avoid making.

- **A container that is not running records `null`, not 0.** The tick still
  appears in the series, so a stack that died mid-run is visible rather than
  looking cheap.

Also carried for the vector-store: its index `count` and `status` next to its
RSS and cgroup memory limit. `vector-store/src/memory.rs` stops adding
documents once its memory budget is reached, logs an error and keeps answering
queries (see SIZING.md), so an undersized run yields a partially-indexed corpus
with plausible, wrong recall. A doc count that stops advancing while
`rss_bytes` sits at `mem_limit_bytes` is that failure, and it is invisible in
C4 unless both numbers are in the same record.
"""
import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import runmeta
from .runmeta import Stopper
from .samplers import OpenSearchSampler, ScyllaSampler

CGROUP_ROOT = Path("/sys/fs/cgroup")
DOCKER_TIMEOUT_S = 10.0
DEFAULT_INTERVAL_S = 1.0
SLEEP_SLICE_S = 0.25
ROLES = ("opensearch", "scylladb", "vector-store")
DOCKER_STATS_FORMAT = "{{.MemUsage}}|{{.BlockIO}}"
INDEX_SIZE_SOURCES = {
    "measured": "opensearch _stats total.store.size_in_bytes",
    "unmeasured": ("unmeasured: no --os-url was given, so index_size_bytes is "
                   "null for want of a reading, not for want of an index"),
    "none": ("none: the Tantivy index lives in RAM, so there is no on-disk "
             "index size; its cost appears in the vector-store's rss_bytes"),
}
RSS_SOURCE_NOTE = ("cgroup v2 memory.stat 'anon' "
                   "(fallback: docker stats MEM USAGE, which is not anon-only)")
BYTE_UNITS = {
    "b": 1, "kb": 10 ** 3, "mb": 10 ** 6, "gb": 10 ** 9, "tb": 10 ** 12,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}
HUMAN_BYTES_RE = re.compile(r"([0-9.]+)\s*([A-Za-z]+)")


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    role: str


@dataclass(frozen=True)
class Counters:
    """One container's raw counters at one instant. Every field is optional:
    absent is how "not measurable" is reported, since zero would be a claim."""
    running: bool
    source: str
    rss_bytes: int | None = None
    cache_bytes: int | None = None
    mem_limit_bytes: int | None = None
    cpu_seconds_total: float | None = None
    disk_read_bytes: int | None = None
    disk_write_bytes: int | None = None


@dataclass(frozen=True)
class Tick:
    i: int
    t_elapsed_s: float


@dataclass(frozen=True)
class Reading:
    counters: Counters
    cpu_cores_used: float | None
    index_size_bytes: int | None
    extras: dict[str, Any]


ABSENT = Counters(running=False, source="absent")


def keyed_values(text: str) -> dict[str, int]:
    """Parse a cgroup v2 `key value` file (memory.stat, cpu.stat)."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            values[parts[0]] = int(parts[1])
    return values


def io_line_bytes(line: str) -> tuple[int, int]:
    fields = dict(pair.split("=", 1) for pair in line.split()[1:] if "=" in pair)
    return int(fields.get("rbytes", 0)), int(fields.get("wbytes", 0))


def io_totals(io_stat: str) -> tuple[int, int]:
    """Summed across devices. Stacked device-mapper layers report the same I/O
    at every level, so on an overlay/dm host this over-counts — which is why
    disk is context in C4 and not one of its bars."""
    pairs = [io_line_bytes(line) for line in io_stat.splitlines() if line.strip()]
    return sum(read for read, _ in pairs), sum(write for _, write in pairs)


def parse_human_bytes(text: str) -> int | None:
    """docker prints memory in IEC units and block I/O in SI units, so both
    tables are needed: reading GiB off a GB table is a silent 7% error."""
    match = HUMAN_BYTES_RE.fullmatch(text.strip())
    if match is None or match.group(2).lower() not in BYTE_UNITS:
        return None
    return int(float(match.group(1)) * BYTE_UNITS[match.group(2).lower()])


def read_cgroup_file(cgroup_dir: Path, name: str) -> str | None:
    try:
        return (cgroup_dir / name).read_text(encoding="utf-8")
    except OSError:
        return None


def cpu_seconds_total(cgroup_dir: Path) -> float | None:
    text = read_cgroup_file(cgroup_dir, "cpu.stat")
    usage_usec = None if text is None else keyed_values(text).get("usage_usec")
    return None if usage_usec is None else usage_usec / 1_000_000.0


def memory_limit_bytes(cgroup_dir: Path) -> int | None:
    """None when the cgroup is unlimited. Recorded because the vector-store
    derives its index budget from this number, and SIZING.md's silent
    truncation happens as `rss_bytes` approaches it."""
    text = read_cgroup_file(cgroup_dir, "memory.max")
    if text is None or text.strip() == "max":
        return None
    return int(text.strip())


def read_cgroup_counters(cgroup_dir: Path) -> Counters:
    memory_stat = read_cgroup_file(cgroup_dir, "memory.stat")
    if memory_stat is None:
        return ABSENT
    memory = keyed_values(memory_stat)
    disk_read, disk_write = io_totals(read_cgroup_file(cgroup_dir, "io.stat") or "")
    return Counters(
        running=True, source="cgroup-anon",
        rss_bytes=memory.get("anon"), cache_bytes=memory.get("file"),
        mem_limit_bytes=memory_limit_bytes(cgroup_dir),
        cpu_seconds_total=cpu_seconds_total(cgroup_dir),
        disk_read_bytes=disk_read, disk_write_bytes=disk_write,
    )


def run_docker(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(["docker", *argv], capture_output=True, text=True,
                                timeout=DOCKER_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def read_docker_stats_counters(name: str) -> Counters:
    """Fallback for hosts where /sys/fs/cgroup is not reachable (rootless
    Docker, or a probe running inside a container). Its `MEM USAGE` is **not**
    anon-only — docker reports `memory.current` less `inactive_file`, so part
    of the page cache is still in there, and it exposes no cumulative CPU
    counter at all. `source` records which path produced each number so a run
    that fell back cannot be mistaken for one that did not."""
    line = run_docker(["stats", "--no-stream", "--format", DOCKER_STATS_FORMAT, name])
    if line is None:
        return ABSENT
    memory, _, block_io = line.partition("|")
    read_bytes, write_bytes = split_docker_pair(block_io)
    return Counters(running=True, source="docker-stats-memusage",
                    rss_bytes=parse_human_bytes(memory.split("/")[0]),
                    disk_read_bytes=read_bytes, disk_write_bytes=write_bytes)


def split_docker_pair(text: str) -> tuple[int | None, int | None]:
    parts = text.split("/")
    if len(parts) != 2:
        return None, None
    return parse_human_bytes(parts[0]), parse_human_bytes(parts[1])


def container_pid(name: str) -> int | None:
    pid = run_docker(["inspect", "--format", "{{.State.Pid}}", name])
    if pid is None or not pid.isdigit() or int(pid) == 0:
        return None
    return int(pid)


def cgroup_dir_for_pid(pid: int | None) -> Path | None:
    """The container's own answer, so it is right under systemd, plain cgroupfs
    and custom cgroup parents alike, without this module guessing a layout."""
    if pid is None:
        return None
    text = read_cgroup_file(Path(f"/proc/{pid}"), "cgroup")
    if text is None or not text.strip():
        return None
    return CGROUP_ROOT / text.strip().rsplit(":", 1)[-1].lstrip("/")


def cgroup_dirs_for_id(container_id: str | None) -> list[Path]:
    if container_id is None:
        return []
    return [CGROUP_ROOT / "system.slice" / f"docker-{container_id}.scope",
            CGROUP_ROOT / "docker" / container_id]


def has_memory_stat(cgroup_dir: Path) -> bool:
    return (cgroup_dir / "memory.stat").exists()


def resolve_cgroup_dir(name: str) -> Path | None:
    candidates = [cgroup_dir_for_pid(container_pid(name)),
                  *cgroup_dirs_for_id(run_docker(
                      ["inspect", "--format", "{{.Id}}", name]))]
    return next((path for path in candidates
                 if path is not None and has_memory_stat(path)), None)


class CgroupLocator:
    """Caches the resolved cgroup directory per container. Resolving it costs
    two `docker inspect` forks, and this probe shares the machine with the
    engine it is measuring — its own overhead is part of the measurement."""

    def __init__(self) -> None:
        self._dirs: dict[str, Path] = {}

    def locate(self, name: str) -> Path | None:
        cached = self._dirs.get(name)
        if cached is not None and has_memory_stat(cached):
            return cached
        found = resolve_cgroup_dir(name)
        if found is not None:
            self._dirs[name] = found
        return found


def read_counters(name: str, locator: CgroupLocator) -> Counters:
    cgroup_dir = locator.locate(name)
    if cgroup_dir is None:
        return read_docker_stats_counters(name)
    return read_cgroup_counters(cgroup_dir)


def cores_used(previous: tuple[float, float], cpu_seconds: float,
               t_elapsed_s: float) -> float | None:
    prev_cpu, prev_t = previous
    delta_t = t_elapsed_s - prev_t
    if delta_t <= 0:
        return None
    return max(0.0, (cpu_seconds - prev_cpu) / delta_t)


class CpuRateTracker:
    """Holds the previous tick's monotonic counter per container.

    The first tick returns None rather than 0.0: there is no earlier counter to
    difference against, and a zero would be indistinguishable from an idle
    container in the C4 CPU bar. The same applies after any gap where the
    counter was unreadable.
    """

    def __init__(self) -> None:
        self._previous: dict[str, tuple[float, float]] = {}

    def rate(self, name: str, cpu_seconds: float | None,
             t_elapsed_s: float) -> float | None:
        previous = self._previous.get(name)
        if cpu_seconds is None:
            return None
        self._previous[name] = (cpu_seconds, t_elapsed_s)
        if previous is None:
            return None
        return cores_used(previous, cpu_seconds, t_elapsed_s)


def guarded(call: Callable[[], dict], what: str) -> dict:
    """An engine endpoint that fails yields no value, not a wrong one."""
    try:
        return call()
    except Exception as err:
        print(f"{what} probe failed: {err}", file=sys.stderr)
        return {}


class EngineProbes:
    """Engine-reported figures that sit beside the cgroup counters."""

    def __init__(self, os_sampler: OpenSearchSampler | None,
                 vs_sampler: ScyllaSampler | None) -> None:
        self._os = os_sampler
        self._vs = vs_sampler

    def index_size_for(self, role: str) -> int | None:
        """`null` for every role but OpenSearch. The Tantivy index is RAM-only,
        so there is no on-disk size to report and a synthesised 0 would claim
        the index is free; its cost is the vector-store's `rss_bytes`."""
        if role != "opensearch" or self._os is None:
            return None
        return guarded(self._os.sample, "opensearch _stats").get("store_size_bytes")

    def extras_for(self, role: str) -> dict[str, Any]:
        if role != "vector-store" or self._vs is None:
            return {}
        sample = guarded(self._vs.sample, "vector-store status")
        return {"index_docs": sample.get("docs_indexed"),
                "index_status": sample.get("index_status")}

    def version(self, engine: str) -> str:
        sampler = self._os if engine == "opensearch" else self._vs
        return sampler.version() if sampler is not None else "unknown"


def read_container(spec: ContainerSpec, tick: Tick, locator: CgroupLocator,
                   rates: CpuRateTracker, probes: EngineProbes) -> Reading:
    counters = read_counters(spec.name, locator)
    return Reading(
        counters=counters,
        cpu_cores_used=rates.rate(spec.name, counters.cpu_seconds_total,
                                  tick.t_elapsed_s),
        index_size_bytes=probes.index_size_for(spec.role),
        extras=probes.extras_for(spec.role),
    )


def build_record(tick: Tick, spec: ContainerSpec, reading: Reading) -> dict[str, Any]:
    counters = reading.counters
    return {
        "record": "resource_sample",
        "i": tick.i,
        "t_elapsed_s": round(tick.t_elapsed_s, 3),
        "container": spec.name,
        "role": spec.role,
        "running": counters.running,
        "source": counters.source,
        "rss_bytes": counters.rss_bytes,
        "cache_bytes": counters.cache_bytes,
        "mem_limit_bytes": counters.mem_limit_bytes,
        "cpu_seconds_total": counters.cpu_seconds_total,
        "cpu_cores_used": reading.cpu_cores_used,
        "disk_read_bytes": counters.disk_read_bytes,
        "disk_write_bytes": counters.disk_write_bytes,
        "index_size_bytes": reading.index_size_bytes,
        **reading.extras,
    }


def mib(value: int | None) -> str:
    return "n/a" if value is None else f"{value / 1024 ** 2:.0f}MiB"


def cores(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def print_tick(tick: Tick, records: list[dict[str, Any]]) -> None:
    parts = [f"{record['role']}: rss={mib(record['rss_bytes'])} "
             f"cpu={cores(record['cpu_cores_used'])}" for record in records]
    print(f"t={tick.t_elapsed_s:>7.1f}s  " + "  ".join(parts), file=sys.stderr)


def parse_container_spec(text: str) -> ContainerSpec:
    name, separator, role = text.partition(":")
    if not separator or role not in ROLES:
        raise argparse.ArgumentTypeError(
            f"expected name:role with role in {ROLES}, got {text!r}")
    return ContainerSpec(name=name, role=role)


def require_vector_store(engine: str, specs: list[ContainerSpec]) -> None:
    """ScyllaDB FTS runs a ScyllaDB cluster *plus* a vector-store cluster
    holding the in-RAM index. A C4 bar built without the vector-store
    understates the ScyllaDB side, so refuse to produce the artifact."""
    if engine != "scylladb":
        return
    if not any(spec.role == "vector-store" for spec in specs):
        raise SystemExit(
            "a scylladb run must sample the vector-store too: add "
            "--containers <name>:vector-store")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("opensearch", "scylladb"), required=True)
    parser.add_argument("--containers", action="append", required=True,
                        type=parse_container_spec, metavar="NAME:ROLE",
                        help=f"repeatable; ROLE in {ROLES}")
    parser.add_argument("--output", required=True, help="JSONL time series path")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                        help="seconds between ticks")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after this many seconds (0 = until signalled)")
    parser.add_argument("--label", default="", help="free-form run label")
    parser.add_argument("--cache-state", default="unspecified",
                        help="cold|warm|unspecified — recorded for the chart footer")
    parser.add_argument("--corpus", default="", help="corpus path, recorded")
    parser.add_argument("--os-url", default="", help="OpenSearch URL for index size")
    parser.add_argument("--os-index", default="wiki-articles")
    parser.add_argument("--vs-url", default="",
                        help="vector-store URL for index count/status")
    parser.add_argument("--keyspace", default="wiki")
    parser.add_argument("--vs-index", default="articles_body_fts")
    return parser.parse_args()


def index_size_source(args: argparse.Namespace) -> str:
    """`index_size_bytes: null` has two very different meanings — an OpenSearch
    index whose size was never read, or a ScyllaDB run that has no on-disk index
    to read — and the samples alone cannot tell them apart. The header says
    which, so a missing bar in C4 stays distinguishable from a free one."""
    if args.engine != "opensearch":
        return INDEX_SIZE_SOURCES["none"]
    return INDEX_SIZE_SOURCES["measured" if args.os_url else "unmeasured"]


def warn_on_unmeasurable_index_size(args: argparse.Namespace) -> None:
    if args.engine == "opensearch" and not args.os_url:
        print("WARNING: --os-url not given, so index_size_bytes stays null and "
              "C4's OpenSearch index-size bar has no data", file=sys.stderr)


def build_probes(args: argparse.Namespace) -> EngineProbes:
    os_sampler = OpenSearchSampler(args.os_url, args.os_index) if args.os_url else None
    vs_sampler = (ScyllaSampler(args.vs_url, args.keyspace, args.vs_index)
                  if args.vs_url else None)
    return EngineProbes(os_sampler, vs_sampler)


def build_header(args: argparse.Namespace, probes: EngineProbes) -> dict[str, Any]:
    return runmeta.header(
        producer="resource_probe", engine=args.engine,
        engine_version=probes.version(args.engine), label=args.label,
        cache_state=args.cache_state, corpus=args.corpus,
        interval_s=args.interval, duration_s=args.duration,
        rss_source=RSS_SOURCE_NOTE,
        index_size_source=index_size_source(args),
        containers=[{"container": spec.name, "role": spec.role}
                    for spec in args.containers],
    )


def expired(duration_s: float, started_s: float) -> bool:
    return duration_s > 0 and time.perf_counter() - started_s >= duration_s


def sleep_until(deadline_s: float, stopper: Stopper) -> None:
    """Sliced so a SIGTERM between ticks is honoured promptly: PEP 475 resumes
    an interrupted sleep, so one long sleep would delay shutdown by a whole
    interval — and this process is started in the background beside a run."""
    while not stopper.stop:
        remaining = deadline_s - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(remaining, SLEEP_SLICE_S))


def emit_tick(out: Any, tick: Tick, specs: list[ContainerSpec],
              locator: CgroupLocator, rates: CpuRateTracker,
              probes: EngineProbes) -> None:
    records = [build_record(tick, spec,
                            read_container(spec, tick, locator, rates, probes))
               for spec in specs]
    for record in records:
        runmeta.write_record(out, record)
    print_tick(tick, records)


def run_ticks(out: Any, args: argparse.Namespace, probes: EngineProbes) -> int:
    stopper = Stopper()
    locator, rates = CgroupLocator(), CpuRateTracker()
    started = time.perf_counter()
    ticks = 0
    while not stopper.stop and not expired(args.duration, started):
        emit_tick(out, Tick(i=ticks, t_elapsed_s=time.perf_counter() - started),
                  args.containers, locator, rates, probes)
        ticks += 1
        sleep_until(started + ticks * args.interval, stopper)
    return ticks


def main() -> int:
    args = parse_args()
    require_vector_store(args.engine, args.containers)
    warn_on_unmeasurable_index_size(args)
    probes = build_probes(args)
    with open(args.output, "w", encoding="utf-8") as out:
        runmeta.write_record(out, build_header(args, probes))
        ticks = run_ticks(out, args, probes)
    print(f"wrote {ticks} ticks x {len(args.containers)} containers "
          f"to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
