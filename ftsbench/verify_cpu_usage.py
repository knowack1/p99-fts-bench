"""Check whether the engines actually used the CPU they were given.

A build-rate ceiling is only an engine result if the engine was CPU-saturated
when it flattened. If a container plateaus at half its quota, the ceiling is
somewhere else — a lock, a single-threaded stage, an I/O wait — and reporting it
as the engine's throughput limit is wrong.

So this reads two things and puts them side by side:

  - the *quota*, from `docker inspect`: NanoCpus (the CFS ceiling) and
    CpusetCpus (which cores it may run on). The effective limit is the smaller.
  - the *observed* `cpu_cores_used`, from the resource_probe series recorded
    while the load ran.

The verdict per container is the ratio of the two at peak. Anything well under
1.0 at the concurrency where the build rate flattened means the ceiling was not
CPU.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
from dataclasses import dataclass

PROBE_RE = re.compile(r"cpu-(?P<config>.+)-c(?P<concurrency>\d+)-(?P<rep>\d+)\.jsonl$")
SATURATED_AT = 0.85


@dataclass(frozen=True)
class Quota:
    nano_cpus: float
    cpuset: str

    @property
    def cpuset_count(self) -> int:
        if not self.cpuset:
            return 0
        total = 0
        for part in self.cpuset.split(","):
            if "-" in part:
                low, high = part.split("-")
                total += int(high) - int(low) + 1
            else:
                total += 1
        return total

    @property
    def effective(self) -> float:
        limits = [x for x in (self.nano_cpus, float(self.cpuset_count)) if x > 0]
        return min(limits) if limits else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/sweep")
    parser.add_argument("--containers", nargs="+",
                        default=["fts-bench-scylla", "fts-bench-vector-store"])
    parser.add_argument("--output-json", default="")
    return parser.parse_args()


def inspect_quota(container: str) -> Quota | None:
    try:
        raw = subprocess.run(
            ["docker", "inspect", container, "--format",
             "{{.HostConfig.NanoCpus}}|{{.HostConfig.CpusetCpus}}"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    nano, _, cpuset = raw.partition("|")
    return Quota(nano_cpus=int(nano or 0) / 1e9, cpuset=cpuset.strip())


def peak_usage(path: str) -> dict[str, float]:
    """Peak, not mean: a build has a ramp and a tail, and the mean over both
    understates what the engine reached while it was actually building."""
    peaks: dict[str, float] = {}
    with open(path) as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("record") != "resource_sample":
                continue
            name = record.get("container", "")
            used = record.get("cpu_cores_used") or 0.0
            peaks[name] = max(peaks.get(name, 0.0), used)
    return peaks


def collect(data_dir: str) -> dict[int, dict[str, float]]:
    by_concurrency: dict[int, dict[str, float]] = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "cpu-*.jsonl"))):
        match = PROBE_RE.search(os.path.basename(path))
        if not match or int(match.group("rep")) < 1:
            continue
        concurrency = int(match.group("concurrency"))
        bucket = by_concurrency.setdefault(concurrency, {})
        for name, used in peak_usage(path).items():
            bucket[name] = max(bucket.get(name, 0.0), used)
    return by_concurrency


def report(by_concurrency: dict, quotas: dict[str, Quota | None]) -> dict:
    print(f"{'conc':>5}  " + "  ".join(f"{name.replace('fts-bench-',''):>28}"
                                       for name in quotas))
    print(f"{'':>5}  " + "  ".join(
        f"{'quota ' + fmt_quota(quotas[name]):>28}" for name in quotas))
    print("-" * (7 + 30 * len(quotas)))
    rows = []
    for concurrency in sorted(by_concurrency):
        cells, row = [], {"concurrency": concurrency, "containers": {}}
        for name in quotas:
            used = by_concurrency[concurrency].get(name, 0.0)
            quota = quotas[name]
            limit = quota.effective if quota else 0.0
            ratio = used / limit if limit else 0.0
            cells.append(f"{used:>10.2f} cores ({ratio * 100:>5.1f}%)")
            row["containers"][name] = {
                "peak_cores_used": round(used, 3),
                "quota_cores": limit,
                "utilisation": round(ratio, 3),
                "saturated": ratio >= SATURATED_AT,
            }
        print(f"{concurrency:>5}  " + "  ".join(f"{c:>28}" for c in cells))
        rows.append(row)
    return {"saturated_at": SATURATED_AT,
            "quotas": {n: quota_dict(q) for n, q in quotas.items()},
            "by_concurrency": rows}


def fmt_quota(quota: Quota | None) -> str:
    if not quota:
        return "n/a"
    return f"{quota.effective:g} (cpuset {quota.cpuset or 'all'})"


def quota_dict(quota: Quota | None) -> dict:
    if not quota:
        return {}
    return {"nano_cpus": quota.nano_cpus, "cpuset": quota.cpuset,
            "cpuset_count": quota.cpuset_count, "effective_cores": quota.effective}


def main() -> int:
    args = parse_args()
    by_concurrency = collect(args.data_dir)
    if not by_concurrency:
        print(f"no cpu-*.jsonl probe series in {args.data_dir}")
        return 1
    quotas = {name: inspect_quota(name) for name in args.containers}
    summary = report(by_concurrency, quotas)
    if args.output_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
        with open(args.output_json, "w") as handle:
            json.dump(summary, handle, indent=1)
        print(f"\nwrote {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
