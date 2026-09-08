"""Assert that the engine took the tuning the arm asked for.

An environment variable the image ignores looks exactly like one that took
effect: public vector-store 1.10.0 accepts every `VECTOR_STORE_FTS_*` variable
and silently does nothing with it, and the campaign already lost S11-S15 to a
writer buffer everyone believed was set. The knobs are also the only thing
separating five arms whose artifacts are otherwise identical, so an arm that
inherited the previous arm's buffer would produce a complete, plausible,
wrongly-labelled ladder.

So the intent in `ftsbench.target` is checked against what the process itself
reports. The vector-store states its tuning when it opens an index:

    fts: ingest tuning for wiki.articles_body_fts: commit_interval=3s
        commit_threshold=disabled add_lock=exclusive dispatch=worker-pool
        metrics_interval=Some(1s)
    fts: index writer using 4 tantivy worker threads, 376 MB buffer per thread,
        4 merge threads (tokio workers=4, available_parallelism=4)

The second line is also the re-run gate `WRITE-PATH-TEST-PLAN.md` asks for: the
376 value was derived assuming four tokio workers, and the worker count is to be
read here rather than assumed.
"""
from __future__ import annotations

import argparse
import re
import sys

from ftsbench import target

TUNING_RE = re.compile(
    r"ingest tuning for \S+: commit_interval=(?P<commit_interval>\S+) "
    r"commit_threshold=(?P<commit_threshold>\S+) "
    r"add_lock=(?P<add_lock>\S+)(?: dispatch=(?P<dispatch>\S+))?"
    r" metrics_interval=(?P<metrics_interval>\S+)")
WRITER_RE = re.compile(
    r"index writer using (?P<worker_threads>\d+) tantivy worker threads, "
    r"(?P<buffer_mb>\d+) MB buffer per thread")

# What the vector-store prints for a knob that is off, versus the harness
# spelling of the same thing.
DISABLED = "disabled"
TANTIVY_FLOOR_MB = 15
DEFAULT_COMMIT_INTERVAL = "3s"


def observed(log_text: str) -> dict[str, str]:
    """The last tuning the process reported, since the index is recreated per
    point and only the most recent statement describes the point at hand."""
    found: dict[str, str] = {}
    for pattern in (TUNING_RE, WRITER_RE):
        matches = list(pattern.finditer(log_text))
        if matches:
            found.update({k: v for k, v in matches[-1].groupdict().items()
                          if v is not None})
    return found


def expected(arm: target.Target) -> dict[str, str]:
    knobs = dict(arm.env)
    buffer_mb = knobs.get("VS_FTS_WRITER_MEMORY_MB", "")
    threshold = knobs.get("VS_FTS_COMMIT_THRESHOLD", "")
    return {
        "buffer_mb": buffer_mb or str(TANTIVY_FLOOR_MB),
        "commit_interval": (knobs.get("VS_FTS_COMMIT_INTERVAL", "")
                            or DEFAULT_COMMIT_INTERVAL),
        "commit_threshold": DISABLED if threshold == "0" else threshold,
    }


def disagreements(arm: target.Target, log_text: str) -> list[str]:
    seen, want = observed(log_text), expected(arm)
    if not seen:
        return [f"{arm.config}: the vector-store never stated its ingest "
                f"tuning; the index was not opened in this window, or the "
                f"image predates the tunables build"]
    problems = []
    for field, wanted in want.items():
        if not wanted:
            continue
        got = seen.get(field)
        if got is None:
            problems.append(f"{arm.config}: {field} not reported")
        elif got != wanted:
            problems.append(f"{arm.config}: {field} is {got!r}, arm asks for "
                            f"{wanted!r}")
    return problems


def worker_note(log_text: str) -> str:
    """The 376 MB/thread value is a *total* budget divided by the worker count.
    If that count is not four, the total is not what the parity argument
    claimed, and the number needs rederiving rather than reusing."""
    seen = observed(log_text)
    threads, buffer_mb = seen.get("worker_threads"), seen.get("buffer_mb")
    if not threads or not buffer_mb:
        return ""
    total = int(threads) * int(buffer_mb)
    return (f"{threads} worker threads x {buffer_mb} MB = {total} MB total "
            f"writer budget")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target.add_target_args(parser)
    parser.add_argument("--log", required=True,
                        help="vector-store log captured for this arm")
    args = parser.parse_args()
    arm = target.resolve(args)

    with open(args.log, encoding="utf-8", errors="replace") as stream:
        text = stream.read()

    note = worker_note(text)
    if note:
        print(f"{arm.config}: {note}")
    problems = disagreements(arm, text)
    for problem in problems:
        print(f"ARM MISMATCH: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
