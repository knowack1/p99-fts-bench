#!/usr/bin/env python3
"""Summarise an inline_dispatch_ab.sh run: the two arms side by side.

Reports the headline build rate per arm, plus the two things that decide what a
rate change *means* -- the vector-store's CPU (a rate that rose because a
serialisation point went away looks nothing like one that rose because per-
document overhead went away) and its peak RSS against the cgroup limit (the
vector-store stops adding documents at its memory budget and keeps answering
queries, so a breach is silent document skipping rather than an error).

Medians, never means: one straggler tail moves a mean and the campaign's own
history is full of them.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

GIB = 1024**3


def read_records(path: Path) -> tuple[dict, list[dict]]:
    header, samples = {}, []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record") == "header":
            header = record
        elif record.get("record") == "sample":
            samples.append(record)
    return header, samples


def build_rate(path: Path) -> tuple[float | None, int]:
    """docs/s to the last indexed document, and the final document count.

    `docs_per_s_cumulative` on the last sample is exactly documents/wall, which
    is the figure the deck's build-rate charts use.
    """
    _, samples = read_records(path)
    if not samples:
        return None, 0
    last = samples[-1]
    return last.get("docs_per_s_cumulative"), last.get("docs_indexed", 0)


def probe_peaks(path: Path) -> dict[str, dict[str, float]]:
    """Per-role peak CPU and RSS, plus the cgroup limit it is measured against."""
    peaks: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cpu": 0.0, "rss": 0.0, "limit": 0.0}
    )
    if not path.exists():
        return {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        role = record.get("role")
        if role is None:
            continue
        entry = peaks[role]
        for key, field in (("cpu", "cpu_cores_used"), ("rss", "rss_bytes"),
                           ("limit", "mem_limit_bytes")):
            value = record.get(field)
            if value is not None:
                entry[key] = max(entry[key], float(value))
    return dict(peaks)


def collect(data_dir: Path, arm: str) -> list[dict]:
    reps = []
    for series in sorted(data_dir.glob(f"c1-{arm}-*.jsonl")):
        rep = series.stem.rsplit("-", 1)[-1]
        rate, docs = build_rate(series)
        reps.append({
            "rep": rep,
            "rate": rate,
            "docs": docs,
            "peaks": probe_peaks(data_dir / f"cpu-{arm}-{rep}.jsonl"),
        })
    return reps


def median_of(reps: list[dict], key: str) -> float | None:
    values = [r[key] for r in reps if r.get(key) is not None]
    return statistics.median(values) if values else None


def median_peak(reps: list[dict], role: str, metric: str) -> float | None:
    values = [r["peaks"][role][metric] for r in reps
              if role in r["peaks"] and r["peaks"][role][metric]]
    return statistics.median(values) if values else None


def format_arm(arm: str, reps: list[dict], expected_docs: int) -> list[str]:
    if not reps:
        return [f"  {arm}: no reps found"]
    rates = ", ".join(f"{r['rate']:,.0f}" if r["rate"] else "n/a" for r in reps)
    lines = [
        f"  {arm:3s}  N={len(reps)}  docs/s per rep: {rates}",
        f"       median: {median_of(reps, 'rate'):,.0f} docs/s",
    ]
    roles = sorted({role for r in reps for role in r["peaks"]})
    for role in roles:
        cpu = median_peak(reps, role, "cpu")
        rss = median_peak(reps, role, "rss")
        limit = median_peak(reps, role, "limit")
        if cpu is None and rss is None:
            continue
        rss_txt = f"{rss / GIB:.1f} GiB" if rss else "n/a"
        limit_txt = f" of {limit / GIB:.0f} GiB limit" if limit else ""
        cpu_txt = f"{cpu:.2f} cores" if cpu else "n/a"
        lines.append(f"       {role:13s} peak CPU {cpu_txt:12s} peak RSS {rss_txt}{limit_txt}")
    short = [r for r in reps if r["docs"] != expected_docs]
    if short:
        lines.append(
            f"       !! GATE: {len(short)} rep(s) short of {expected_docs:,} docs: "
            + ", ".join(f"rep{r['rep']}={r['docs']:,}" for r in short)
        )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/inline-ab")
    parser.add_argument("--expected-docs", type=int, default=1_000_000)
    parser.add_argument("--baseline-arm", default="off")
    parser.add_argument("--variant-arm", default="on")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    arms = {arm: collect(data_dir, arm)
            for arm in (args.baseline_arm, args.variant_arm)}

    print(f"\nA/B report — {data_dir}\n" + "=" * 60)
    for arm, reps in arms.items():
        print("\n".join(format_arm(arm, reps, args.expected_docs)))

    base = median_of(arms[args.baseline_arm], "rate")
    variant = median_of(arms[args.variant_arm], "rate")
    print("\n" + "-" * 60)
    if base and variant:
        delta = (variant / base - 1) * 100
        print(f"  {args.variant_arm} vs {args.baseline_arm}: "
              f"{variant:,.0f} vs {base:,.0f} docs/s  =  {variant / base:.3f}x "
              f"({delta:+.1f}%)")
        # The CDC path's measured rep-to-rep spread on this fleet is ~3%; a
        # difference under that is not a result, it is the box.
        if abs(delta) < 3:
            print("  -> WITHIN NOISE (~3% CDC rep-to-rep spread): not a result")
        else:
            print(f"  -> outside the ~3% noise band")
    else:
        print("  insufficient data for a comparison")
    print()


if __name__ == "__main__":
    main()
