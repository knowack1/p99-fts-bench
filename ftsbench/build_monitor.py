"""Index-build throughput monitor — the time series behind chart C1.

Polls an engine at a fixed wall-clock interval and writes one JSONL record
per sample with instantaneous docs/s. Run it alongside a loader:

    python3 -m ftsbench.build_monitor --engine opensearch \\
        --output data/c1-opensearch.jsonl --idle-timeout 60 &
    python3 -m ftsbench.opensearch_load --corpus data/corpus.jsonl

Two design points that matter for C1 correctness:

- **Fixed wall-clock sampling, not per-N-docs.** If throughput collapses
  during a merge, doc-triggered reporting goes sparse exactly where the chart
  needs resolution.
- **Instantaneous rate, not cumulative.** The sawtooth is the finding; a
  running average erases it.

Stops on: --until-docs reached *and searchable* (and index SERVING, for
ScyllaDB), no progress for --idle-timeout seconds, --max-seconds elapsed, or
SIGINT.

The searchable half of that condition is not a refinement. OpenSearch makes
documents findable a refresh interval behind the write, so stopping when the
last document is *written* ends the series one refresh short: the first pass
over this corpus lost 22,554 of 270,269 documents at refresh=1s, and at
refresh=30s the build finished in 13 seconds with no document ever searchable.
C2 is measured off this series, so that shortfall is the whole answer, not a
rounding error.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

from . import runmeta
from .runmeta import Stopper
from .samplers import build_sampler

DEFAULT_INTERVAL_S = 1.0
DEFAULT_IDLE_TIMEOUT_S = 120.0
DEFAULT_STARTUP_GRACE_S = 600.0
# Above any refresh_interval the campaign configures, so a settle phase that
# times out means the index stopped refreshing rather than that we were hasty.
DEFAULT_SETTLE_TIMEOUT_S = 120.0


def parse_args() -> argparse.Namespace:
    return parse_args_from(None)


def parse_args_from(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("opensearch", "scylladb"), required=True)
    parser.add_argument("--output", required=True, help="JSONL time series path")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                        help="seconds between samples")
    parser.add_argument("--until-docs", type=int, default=0,
                        help="stop once this many docs are indexed (0 = no target)")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_S,
                        help="stop after this many seconds with no doc progress "
                             "(only armed once the first documents appear)")
    parser.add_argument("--startup-grace", type=float, default=DEFAULT_STARTUP_GRACE_S,
                        help="seconds to wait for the loader to start producing "
                             "documents before giving up")
    parser.add_argument("--max-seconds", type=float, default=0.0,
                        help="hard stop (0 = none)")
    parser.add_argument("--label", default="", help="free-form run label recorded in the header")
    parser.add_argument("--cache-state", default="unspecified",
                        help="cold|warm|unspecified — recorded for the chart footer")
    parser.add_argument("--url", default="http://localhost:9200", help="OpenSearch URL")
    parser.add_argument("--index", default="wiki-articles", help="OpenSearch index")
    parser.add_argument("--vs-url", default="http://localhost:6080",
                        help="vector-store base URL")
    parser.add_argument("--keyspace", default="wiki")
    # The series is the only artifact for C1 and C2, and a build series that
    # does not name the corpus it built cannot be reviewed a month later or
    # told apart from a --smoke run of the same config.
    parser.add_argument("--corpus", default="", help="corpus the build reads")
    parser.add_argument("--vs-index", default="articles_body_fts",
                        help="vector-store index name")
    parser.add_argument("--settle-timeout", type=float,
                        default=DEFAULT_SETTLE_TIMEOUT_S,
                        help="after --until-docs is written, how long to keep "
                             "sampling for it to become searchable")
    return parser.parse_args(argv)


def rate(delta_docs: int, delta_t: float) -> float:
    return delta_docs / delta_t if delta_t > 0 else 0.0


def write_header(out, args, sampler) -> None:
    """Through runmeta so a build series carries the same provenance as a load
    log: corpus, target document count, git commit, host and container facts.
    `max_docs` is what --until-docs was asked for, which is what lets a chart
    refuse to draw a 20k smoke repetition beside a full-corpus one."""
    header = runmeta.header(
        producer="build_monitor", engine=args.engine,
        engine_version=sampler.version(), label=args.label,
        cache_state=args.cache_state, corpus=args.corpus,
        max_docs=args.until_docs, interval_s=args.interval,
        idle_timeout_s=args.idle_timeout, max_seconds=args.max_seconds,
        settle_timeout_s=args.settle_timeout,
    )
    out.write(json.dumps(header) + "\n")
    out.flush()


def report_settle(args, sample: dict, settle_for: float) -> None:
    """A series that ends with documents written but not findable is the one
    thing C2 cannot recover from, so it is said out loud rather than left for
    the chart to render as infinity."""
    shortfall = searchable_shortfall(args, sample)
    if not shortfall:
        return
    print(f"WARNING: {shortfall} of {args.until_docs} documents were written but "
          f"never became searchable within {settle_for:.0f}s "
          f"(--settle-timeout {args.settle_timeout:.0f}s): C2 cannot be "
          "measured from this series", file=sys.stderr)


def print_line(elapsed: float, docs: int, instant: float, sample: dict) -> None:
    extra = ""
    if sample.get("merges_current") is not None:
        extra = (f"  segs={sample.get('segments_count', 0):>4}"
                 f"  merging={sample.get('merges_current', 0):>2}"
                 f"  merges={sample.get('merges_total', 0):>5}")
    status = sample.get("index_status", "")
    if status and status != "n/a":
        extra += f"  status={status}"
    print(f"t={elapsed:>7.1f}s  docs={docs:>10}  {instant:>9.0f} docs/s{extra}",
          file=sys.stderr)


def writing_finished(args, sample: dict, docs: int) -> bool:
    if not args.until_docs or docs < args.until_docs:
        return False
    return sample.get("index_status", "n/a") in ("n/a", "SERVING")


def searchable_shortfall(args, sample: dict) -> int:
    """How many of the target documents are written but not yet findable.

    A sampler that reports no searchable count cannot answer the question, and
    waiting on a field that will never arrive would hang every run — so absence
    reads as nothing outstanding.
    """
    searchable = sample.get("docs_searchable")
    if searchable is None:
        return 0
    return max(0, args.until_docs - int(searchable))


def should_stop(args, sample: dict, docs: int, idle_for: float, elapsed: float,
                seen_progress: bool, settle_for: float = 0.0) -> bool:
    if args.max_seconds and elapsed >= args.max_seconds:
        return True
    if not seen_progress:
        return elapsed >= args.startup_grace
    if writing_finished(args, sample, docs):
        # The idle timeout is deliberately not consulted here: once the loader
        # stops, the indexed count stops moving by construction, so an idle
        # count during the settle phase is the expected state and not a stall.
        if not searchable_shortfall(args, sample):
            return True
        return settle_for >= args.settle_timeout
    return idle_for >= args.idle_timeout


def main() -> int:
    args = parse_args()
    sampler = build_sampler(args)
    stopper = Stopper()

    started = time.perf_counter()
    prev_docs = None
    prev_t = started
    last_progress_at = started
    seen_progress = False
    samples = 0
    writing_finished_at = None

    with open(args.output, "w", encoding="utf-8") as out:
        write_header(out, args, sampler)
        while not stopper.stop:
            now = time.perf_counter()
            try:
                sample = sampler.sample()
            except Exception as err:
                print(f"sample failed: {err}", file=sys.stderr)
                time.sleep(args.interval)
                continue

            docs = sample.get("docs_indexed", 0)
            elapsed = now - started
            delta_docs = 0 if prev_docs is None else docs - prev_docs
            instant = rate(delta_docs, now - prev_t)

            if docs > 0:
                seen_progress = True
            if delta_docs > 0:
                last_progress_at = now

            record = {
                "record": "sample",
                "i": samples,
                "t_elapsed_s": round(elapsed, 3),
                "docs_delta": delta_docs,
                "docs_per_s": round(instant, 1),
                "docs_per_s_cumulative": round(rate(docs, elapsed), 1),
                **sample,
            }
            out.write(json.dumps(record) + "\n")
            out.flush()
            print_line(elapsed, docs, instant, sample)

            prev_docs = docs
            prev_t = now
            samples += 1

            if writing_finished(args, sample, docs) and writing_finished_at is None:
                writing_finished_at = now
            settle_for = 0.0 if writing_finished_at is None else now - writing_finished_at

            if should_stop(args, sample, docs, now - last_progress_at, elapsed,
                           seen_progress, settle_for):
                report_settle(args, sample, settle_for)
                break
            time.sleep(args.interval)

    print(f"wrote {samples} samples to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
