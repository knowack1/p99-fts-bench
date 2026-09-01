"""Summarize a build-monitor time series into chart-C1 inputs.

Reads the JSONL written by ftsbench.build_monitor and reports the numbers C1
needs: sustained throughput, the shape of the throughput curve, and — for
OpenSearch — how much of the wall time was spent merging, which is the
mechanism behind the sawtooth.

Usage: python3 -m ftsbench.build_report data/c1-opensearch.jsonl
"""
import argparse
import json
import statistics
import sys

from .stats import percentile as interpolated_percentile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("series", nargs="+", help="one or more build_monitor JSONL files")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser.parse_args()


def load_series(path: str) -> tuple[dict, list[dict]]:
    header = {}
    samples = []
    with open(path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"{path}:{line_number}: skipping unparseable line",
                      file=sys.stderr)
                continue
            if record.get("record") == "header":
                header = record
            elif record.get("record") == "sample":
                samples.append(record)
    return header, samples


def percentile(values: list[float], pct: float) -> float:
    """One percentile definition for the whole repo.

    This used to be nearest-rank while every latency chart used the
    interpolated definition in ftsbench.stats, so the C1 sidecar's p90 and a
    C5 p90 over the same numbers disagreed slightly — a difference a reader
    would reasonably read as a measurement, not as two algorithms. An empty
    build window is a normal outcome here (see build_window) and reports 0.0
    rather than raising as stats.percentile does for latency samples.
    """
    if not values:
        return 0.0
    return interpolated_percentile(sorted(values), pct)


def build_window(samples: list[dict]) -> tuple[list[dict], float]:
    """Trim to the active build: first doc seen .. last doc added.

    Samples taken before the loader starts and after it finishes are idle by
    construction. Leaving them in drags the median to zero and makes the
    stall fraction meaningless, so they are excluded from rate statistics and
    the trailing idle time is reported separately.
    """
    first = next((i for i, s in enumerate(samples) if s.get("docs_indexed", 0) > 0), None)
    if first is None:
        return [], 0.0
    last = max((i for i, s in enumerate(samples) if s.get("docs_delta", 0) > 0),
               default=None)
    if last is None:
        return [], 0.0
    trailing_idle = samples[-1].get("t_elapsed_s", 0.0) - samples[last].get("t_elapsed_s", 0.0)
    return samples[first:last + 1], trailing_idle


def summarize(header: dict, samples: list[dict]) -> dict:
    if not samples:
        return {"error": "no samples"}
    window, trailing_idle = build_window(samples)
    if not window:
        return {"error": "no documents indexed during this run"}

    rates = [s["docs_per_s"] for s in window]
    total_docs = window[-1].get("docs_indexed", 0)
    build_wall = window[-1].get("t_elapsed_s", 0.0) - window[0].get("t_elapsed_s", 0.0)
    stalls = [r for r in rates if r == 0.0]

    summary = {
        "engine": header.get("engine"),
        "engine_version": header.get("engine_version"),
        "label": header.get("label"),
        "cache_state": header.get("cache_state"),
        "samples_total": len(samples),
        "samples_in_build": len(window),
        "docs_total": total_docs,
        "build_wall_seconds": round(build_wall, 1),
        "trailing_idle_seconds": round(trailing_idle, 1),
        "docs_per_s_overall": round(total_docs / build_wall, 1) if build_wall else 0.0,
        "docs_per_s_mean": round(statistics.mean(rates), 1) if rates else 0.0,
        "docs_per_s_median": round(statistics.median(rates), 1) if rates else 0.0,
        "docs_per_s_p10": round(percentile(rates, 10), 1),
        "docs_per_s_p90": round(percentile(rates, 90), 1),
        "docs_per_s_max": round(max(rates), 1) if rates else 0.0,
        "stall_samples": len(stalls),
        "stall_fraction": round(len(stalls) / len(rates), 4) if rates else 0.0,
    }

    if rates and summary["docs_per_s_median"]:
        summary["throughput_variability"] = round(
            (summary["docs_per_s_p90"] - summary["docs_per_s_p10"])
            / summary["docs_per_s_median"], 3
        )

    merge_samples = [s for s in window if s.get("merges_total") is not None]
    if merge_samples:
        first, last = merge_samples[0], merge_samples[-1]
        merge_ms = last.get("merges_total_time_ms", 0) - first.get("merges_total_time_ms", 0)
        summary["merges_total"] = last.get("merges_total", 0) - first.get("merges_total", 0)
        summary["merges_total_docs"] = (last.get("merges_total_docs", 0)
                                        - first.get("merges_total_docs", 0))
        summary["merge_time_s"] = round(merge_ms / 1000.0, 1)
        summary["merge_time_fraction_of_build"] = (
            round(merge_ms / 1000.0 / build_wall, 4) if build_wall else 0.0
        )
        summary["samples_with_active_merge"] = sum(
            1 for s in merge_samples if s.get("merges_current", 0) > 0
        )
        summary["segments_final"] = last.get("segments_count", 0)
        summary["segments_max"] = max(s.get("segments_count", 0) for s in merge_samples)
        summary["store_size_bytes_final"] = last.get("store_size_bytes", 0)

    statuses = {s.get("index_status") for s in samples} - {None, "n/a"}
    if statuses:
        summary["index_statuses_seen"] = sorted(statuses)
        serving = next((s for s in samples if s.get("index_status") == "SERVING"), None)
        if serving:
            summary["time_to_serving_s"] = serving.get("t_elapsed_s")

    return summary


def print_summary(path: str, summary: dict) -> None:
    print(f"\n=== {path} ===")
    if "error" in summary:
        print(f"  {summary['error']}")
        return
    for key, value in summary.items():
        print(f"  {key:<32} {value}")


def main() -> int:
    args = parse_args()
    results = {}
    for path in args.series:
        header, samples = load_series(path)
        summary = summarize(header, samples)
        results[path] = summary
        if not args.json:
            print_summary(path, summary)
    if args.json:
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
