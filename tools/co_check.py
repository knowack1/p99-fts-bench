"""Coordinated-omission gate: prove the pacer is open-loop before measuring.

A closed-loop generator sends the next request only after the previous one
returns, so it never observes the queueing it caused. Its percentiles then
describe an engine that was never overloaded — which is the single most common
way a latency benchmark reports a number that cannot happen in production.

The test is a behaviour, not an inspection: driven deliberately above capacity,
`latency_ms` (from the INTENDED send time) must diverge sharply from
`service_ms` (from the actual send), and `queue_ms` must account for the gap. A
generator that stays closed-loop cannot produce that divergence, because it
never has more than one request outstanding.

If this gate does not fire, C5 and C7 are worthless and no amount of care in the
charts recovers them.

    python3 tools/co_check.py --engine opensearch --rate 5000

Exit 0 means the pacer is open-loop at the offered rate. Exit 1 means it is not,
or that the rate chosen was not actually above capacity — both block measurement.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
DIVERGENCE_FACTOR = 2.0
QUEUE_SHARE_FLOOR = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True,
                        choices=("opensearch", "scylladb"))
    parser.add_argument("--rate", type=int, required=True,
                        help="offered QPS, deliberately above capacity")
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--queries", default="data/queries.json")
    parser.add_argument("--python", default=".venv/bin/python3")
    return parser.parse_args()


def connection_args(engine: str) -> list[str]:
    if engine == "opensearch":
        return ["--url", "http://localhost:9200", "--index", "wiki-articles"]
    return ["--hosts", "127.0.0.1", "--port", "19042", "--keyspace", "wiki"]


def run_overloaded(args: argparse.Namespace, log_path: Path) -> None:
    command = [
        args.python, "-m", "ftsbench.load_gen",
        "--engine", args.engine, "--queries", args.queries,
        "--rate", str(args.rate), "--duration", str(args.duration),
        "--warmup", "0", "--concurrency", str(args.concurrency),
        "--latency-log", str(log_path),
        "--label", f"coordinated-omission gate at {args.rate} qps",
        "--cache-state", "warm",
        *connection_args(args.engine),
    ]
    subprocess.run(command, cwd=BENCH_DIR, check=True)


def percentiles(log_path: Path) -> dict[str, float]:
    sys.path.insert(0, str(BENCH_DIR))
    from ftsbench.runmeta import read_jsonl
    from ftsbench.stats import percentile

    _, records = read_jsonl(log_path)
    successful = [r for r in records if r.get("record") == "latency_op" and r.get("ok")]
    if len(successful) < 100:
        sys.exit(f"only {len(successful)} successful operations — too few to judge; "
                 "is the engine up and the index populated?")
    return {
        "count": len(successful),
        "latency_p99": percentile([r["latency_ms"] for r in successful], 99),
        "service_p99": percentile([r["service_ms"] for r in successful], 99),
        "queue_p99": percentile([r["queue_ms"] for r in successful], 99),
    }


def report(measured: dict[str, float]) -> None:
    print(json.dumps(measured, indent=2))


def diverged(measured: dict[str, float]) -> bool:
    return measured["latency_p99"] >= measured["service_p99"] * DIVERGENCE_FACTOR


def queue_explains_the_gap(measured: dict[str, float]) -> bool:
    gap = measured["latency_p99"] - measured["service_p99"]
    return gap > 0 and measured["queue_p99"] >= gap * QUEUE_SHARE_FLOOR


def main() -> int:
    args = parse_args()
    with tempfile.TemporaryDirectory() as workdir:
        log_path = Path(workdir) / "co-check.jsonl"
        run_overloaded(args, log_path)
        measured = percentiles(log_path)

    report(measured)
    if not diverged(measured):
        print(f"FAILED: latency p99 {measured['latency_p99']:.1f} ms is not "
              f"{DIVERGENCE_FACTOR}x service p99 {measured['service_p99']:.1f} ms. "
              "Either the pacer is closed-loop, or the offered rate was below "
              "capacity — raise --rate and retry before concluding.", file=sys.stderr)
        return 1
    if not queue_explains_the_gap(measured):
        print("FAILED: latency and service diverged but queue_ms does not account "
              "for the gap, so the harness is not reporting where the time went.",
              file=sys.stderr)
        return 1
    print(f"PASSED: at {args.rate} qps offered, latency p99 "
          f"{measured['latency_p99']:.1f} ms vs service p99 "
          f"{measured['service_p99']:.1f} ms, queue p99 "
          f"{measured['queue_p99']:.1f} ms. The generator is open-loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
