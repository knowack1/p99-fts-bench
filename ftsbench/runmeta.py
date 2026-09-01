"""The header record every harness output starts with (see SCHEMAS.md).

One implementation, imported by every producer. A header that differs between
producers breaks the results-tree generator that builds the per-chart write-ups
from the artifacts, so this is deliberately not something each tool rolls itself.

The env block is what makes a laptop measurement auditable rather than merely
small: a run taken with 9 GB of swap in use, an unpinned CPU affinity, or a
load average of 4 from someone else's containers is still usable, but only if
the artifact says so.
"""
import json
import os
import platform
import signal
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Dirtiness is scoped to bench/: an edit to the talk document cannot change
# what the harness measured, and flagging every run dirty for it would train
# the reader to ignore the flag.
BENCH_ROOT = Path(__file__).resolve().parent.parent


class Stopper:
    """Cooperative shutdown flag for the long-running samplers and probes.

    Lives here rather than in a CLI module because a background probe importing
    a command-line entry point is the wrong dependency direction, and every
    producer needs the same behaviour: a Ctrl-C must end the sampling loop at a
    record boundary so the artifact stays parseable.
    """

    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, _signum, _frame):
        self.stop = True


def total_ram_bytes() -> int:
    """0 when the platform does not expose it — treat as unknown, not zero."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def git_output(*arguments: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT), *arguments],
                                capture_output=True, text=True, timeout=5,
                                check=True)
        return result.stdout
    except Exception:
        return None


def working_tree_is_dirty() -> bool:
    status = git_output("status", "--porcelain", "--untracked-files=no", "--",
                        str(BENCH_ROOT))
    return bool(status and status.strip())


def git_commit() -> str:
    """The commit, suffixed `-dirty` when it is not what actually ran.

    A bare hash on an artifact produced from uncommitted code names a commit
    that does not contain the code that made it, which is worse than recording
    nothing: it invites someone to check out that hash and conclude the numbers
    are reproducible.
    """
    revision = git_output("rev-parse", "--short", "HEAD")
    if revision is None or not revision.strip():
        return "unknown"
    commit = revision.strip()
    return f"{commit}-dirty" if working_tree_is_dirty() else commit


def swap_used_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("SwapFree:"):
                free_kb = int(line.split()[1])
            elif line.startswith("SwapTotal:"):
                total_kb = int(line.split()[1])
        return (total_kb - free_kb) * 1024
    except Exception:
        return -1


def load_avg_1m() -> float:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return -1.0


def cpu_affinity() -> list[int]:
    """The full core list here means the run was NOT pinned — which is the
    finding, on a host whose cores range from 2.5 to 4.8 GHz."""
    try:
        return sorted(os.sched_getaffinity(0))
    except AttributeError:
        return []


def host_facts() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count() or 0,
        "total_ram_bytes": total_ram_bytes(),
    }


def env_facts() -> dict[str, Any]:
    return {
        "swap_used_bytes": swap_used_bytes(),
        "load_avg_1m": load_avg_1m(),
        "cpu_affinity": cpu_affinity(),
    }


def header(producer: str, engine: str, engine_version: str = "unknown",
           label: str = "", cache_state: str = "unspecified",
           corpus: str = "", max_docs: int = 0,
           **extra: Any) -> dict[str, Any]:
    """Build the header record. `extra` carries producer-specific settings that
    belong in the artifact — refresh_interval, batch_size, concurrency, rate —
    so a chart footer can name the tuning that produced it."""
    return {
        "record": "header",
        "schema_version": SCHEMA_VERSION,
        "producer": producer,
        "engine": engine,
        "engine_version": engine_version,
        "label": label,
        "cache_state": cache_state,
        "corpus": corpus,
        "max_docs": max_docs,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "host": host_facts(),
        "env": env_facts(),
        **extra,
    }


def write_record(stream: TextIO, record: dict[str, Any]) -> None:
    """Flush per record: a run killed mid-flight must leave usable data behind."""
    stream.write(json.dumps(record) + "\n")
    stream.flush()


def read_jsonl(path: str | os.PathLike) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split an artifact into (header, records). Malformed lines are skipped
    rather than fatal, so a truncated final line from a killed run does not
    make the whole artifact unreadable."""
    head: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if parsed.get("record") == "header":
                head = parsed
            else:
                records.append(parsed)
    return head, records
