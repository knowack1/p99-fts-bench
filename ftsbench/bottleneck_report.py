"""Build the comparison table for the FTS ingest-bottleneck matrix.

Four numbers per variant, and the point is what they say together:

- **index docs/s** — documents becoming searchable, from `build_monitor`. The
  outcome being explained.
- **CDC received/s** and **added/s** — measured inside the vector-store. If both
  sit far above the index rate, the ingest path is not the constraint and the
  documents are simply not arriving continuously.
- **duty cycle** — how much of the build window the vector-store was actually
  being fed, derived as `total_received / mean_active_rate / build_wall`. A low
  duty cycle with a high active rate means the limit is upstream delivery
  pacing, not ingest capacity.
- **lock wait** — time blocked acquiring the writer lock. This is what would be
  large if the per-document exclusive lock were the constraint.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import statistics
from dataclasses import dataclass, field

from ftsbench import build_report

METRIC_RE = re.compile(
    r"received=(\d+)/s added=(\d+)/s lock_wait=([\d.]+)ms/s "
    r"commits=(\d+) committed=(\d+)/s \(totals received=(\d+) added=(\d+)\)")
TUNING_RE = re.compile(
    r"ingest tuning for \S+: commit_interval=(\S+) commit_threshold=(\S+) "
    r"add_lock=(\S+) metrics_interval=(\S+)")
LOADER_RE = re.compile(r"([\d.]+) docs/s avg")
ACTIVE_FLOOR = 100


@dataclass
class Variant:
    name: str
    reps: list[dict] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/bottleneck")
    parser.add_argument("--output-csv", default="")
    parser.add_argument("--output-md", default="")
    return parser.parse_args()


def index_rate(series_path: str) -> dict:
    header, samples = build_report.load_series(series_path)
    summary = build_report.summarize(header, samples)
    if "error" in summary:
        return {}
    return {
        "index_docs_per_s": summary.get("docs_per_s_overall"),
        "build_wall_s": summary.get("build_wall_seconds"),
        "docs_total": summary.get("docs_total"),
        "label": header.get("label", ""),
    }


def vector_store_metrics(log_path: str, build_wall: float | None) -> dict:
    if not os.path.exists(log_path):
        return {}
    windows, tuning = [], {}
    with open(log_path, errors="replace") as handle:
        for line in handle:
            found = METRIC_RE.search(line)
            if found:
                windows.append([float(x) for x in found.groups()])
                continue
            tune = TUNING_RE.search(line)
            if tune:
                tuning = {"logged_interval": tune.group(1),
                          "logged_threshold": tune.group(2),
                          "logged_lock": tune.group(3)}
    if not windows:
        return tuning
    active = [w for w in windows if w[0] > ACTIVE_FLOOR]
    # Bulk rate and total time are different questions and this matrix showed
    # they diverge by 4x. `docs_per_s_overall` runs to the *last* document, so a
    # handful of stragglers delivered a minute late halve it while bulk ingest
    # was never near its limit. Reporting only one of the two is what made the
    # vector-store look like a ~4k docs/s ceiling.
    total_received = max(w[5] for w in windows)
    result = dict(tuning)
    result["total_received"] = total_received
    if active:
        mean_received = statistics.mean(w[0] for w in active)
        result["active_received_per_s"] = round(mean_received)
        result["active_added_per_s"] = round(statistics.mean(w[1] for w in active))
        result["active_lock_wait_ms_per_s"] = round(
            statistics.mean(w[2] for w in active), 1)
        result["peak_received_per_s"] = round(max(w[0] for w in active))
        # Windows carrying the bulk: the top ones that together hold 95% of the
        # documents. Their mean is the ingest rate the path actually sustains.
        ordered = sorted((w[0] for w in active), reverse=True)
        carried, bulk = 0.0, []
        for rate in ordered:
            bulk.append(rate)
            carried += rate * 5
            if carried >= 0.95 * total_received:
                break
        result["bulk_received_per_s"] = round(statistics.mean(bulk))
        result["tail_windows"] = len(windows) - len(bulk)
        if build_wall and mean_received:
            fed_seconds = total_received / mean_received
            result["duty_cycle"] = round(min(fed_seconds / build_wall, 1.0), 3)
    return result


def loader_rate(log_path: str) -> dict:
    if not os.path.exists(log_path):
        return {}
    rates = LOADER_RE.findall(open(log_path, errors="replace").read())
    return {"loader_docs_per_s": round(float(rates[-1]))} if rates else {}


def cpu_peaks(probe_path: str) -> dict:
    if not os.path.exists(probe_path):
        return {}
    peaks: dict[str, float] = {}
    with open(probe_path) as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("record") != "resource_sample":
                continue
            role = record.get("role", "")
            peaks[role] = max(peaks.get(role, 0.0), record.get("cpu_cores_used") or 0.0)
    return {"vs_cores": round(peaks.get("vector-store", 0.0), 2),
            "scylla_cores": round(peaks.get("scylladb", 0.0), 2)}


def collect(data_dir: str) -> dict[str, Variant]:
    variants: dict[str, Variant] = {}
    for series in sorted(glob.glob(os.path.join(data_dir, "V*.jsonl"))):
        base = os.path.basename(series)[:-len(".jsonl")]
        name, _, rep = base.rpartition("-")
        record = index_rate(series)
        if not record:
            continue
        record["rep"] = rep
        record.update(vector_store_metrics(
            os.path.join(data_dir, f"vslog-{name}-{rep}.log"), record.get("build_wall_s")))
        record.update(loader_rate(os.path.join(data_dir, f"load-{name}-{rep}.log")))
        record.update(cpu_peaks(os.path.join(data_dir, f"cpu-{name}-{rep}.jsonl")))
        variants.setdefault(name, Variant(name)).reps.append(record)
    return variants


def median_of(reps: list[dict], key: str):
    values = [r[key] for r in reps if isinstance(r.get(key), (int, float))]
    return round(statistics.median(values), 1) if values else None


ENV_FROM_LABEL = re.compile(
    r"interval=(\S+) threshold=(\S+) lock=(\S+) cdc=(\S*)/(\S*) cpus=(\S+)")

COLUMNS = [
    ("variant", "variant"),
    ("commit_interval", "VECTOR_STORE_FTS_COMMIT_INTERVAL"),
    ("commit_threshold", "VECTOR_STORE_FTS_COMMIT_THRESHOLD"),
    ("add_lock", "VECTOR_STORE_FTS_ADD_LOCK"),
    ("cdc_fine_sleep", "VECTOR_STORE_CDC_FINE_SLEEP_INTERVAL"),
    ("cdc_fine_safety", "VECTOR_STORE_CDC_FINE_SAFETY_INTERVAL"),
    ("vs_cpus", "VS_CPUS"),
    ("index_docs_per_s", "index docs/s"),
    ("loader_docs_per_s", "loader docs/s"),
    ("bulk_received_per_s", "bulk CDC recv/s"),
    ("active_received_per_s", "CDC recv/s (active)"),
    ("active_added_per_s", "added/s (active)"),
    ("duty_cycle", "duty cycle"),
    ("build_wall_s", "build wall s"),
    ("active_lock_wait_ms_per_s", "lock wait ms/s"),
    ("vs_cores", "VS cores"),
    ("scylla_cores", "scylla cores"),
    ("reps", "N"),
]


def summarise(variants: dict[str, Variant]) -> list[dict]:
    rows = []
    for name, variant in sorted(variants.items()):
        label = variant.reps[0].get("label", "")
        env = ENV_FROM_LABEL.search(label)
        row = {"variant": name, "reps": len(variant.reps)}
        if env:
            row.update({
                "commit_interval": env.group(1),
                "commit_threshold": "disabled" if env.group(2) == "0" else env.group(2),
                "add_lock": env.group(3),
                "cdc_fine_sleep": env.group(4) or "default 500ms",
                "cdc_fine_safety": env.group(5) or "default 100ms",
                "vs_cpus": env.group(6),
            })
        for key in ("index_docs_per_s", "loader_docs_per_s", "bulk_received_per_s",
                    "build_wall_s", "active_received_per_s",
                    "active_added_per_s", "duty_cycle", "active_lock_wait_ms_per_s",
                    "vs_cores", "scylla_cores"):
            row[key] = median_of(variant.reps, key)
        rows.append(row)
    return rows


def render(rows: list[dict]) -> str:
    keys = [k for k, _ in COLUMNS]
    heads = [h for _, h in COLUMNS]
    lines = ["| " + " | ".join(heads) + " |",
             "|" + "|".join("---" for _ in heads) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(
            "" if row.get(k) is None else str(row.get(k)) for k in keys) + " |")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    variants = collect(args.data_dir)
    if not variants:
        print(f"no variant series in {args.data_dir}")
        return 1
    rows = summarise(variants)
    table = render(rows)
    print(table)
    if args.output_csv:
        with open(args.output_csv, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[k for k, _ in COLUMNS])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {args.output_csv}")
    if args.output_md:
        open(args.output_md, "w").write(table + "\n")
        print(f"wrote {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
