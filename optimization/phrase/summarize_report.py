"""Turn one `ftsbench.query_bench` report into CSV rows for the A/B ledger.

Usage: summarize_report.py <report.json> <rep> <variant> <segments>
"""
import json
import sys


def main() -> int:
    report, rep, variant, segments = sys.argv[1:5]
    with open(report, encoding="utf-8") as f:
        data = json.load(f)
    for name, cls in data["classes"].items():
        s = cls["summary"]
        print(f"{rep},{variant},{segments},{name},"
              f"{s['p50_ms']},{s['p95_ms']},{s['p99_ms']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
