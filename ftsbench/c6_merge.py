"""Assemble one repetition's C6 artifact from its six per-class C5 logs.

C5 and C6 are the same measurement read two ways: C5 is the headline class,
C6 is every class side by side. The generator writes one log per class, but
`plotlib` treats one file as one repetition, so C6 needs the six logs of a
repetition in a single file — with the `class` field intact, which is what
`plot_c6` groups on.

Two things this refuses to do quietly:

- **Glob for the inputs.** `data/c5-opensearch-*` also matches
  `c5-opensearch-refresh30-*`, and that mistake has already cost this harness
  one chart. The class list is explicit and the config is a parameter.
- **Skip a missing class.** `plot_c6` draws a class with no data as a labelled
  gap, which reads as a finding about the engine. A missing input file is a
  finding about the harness, so it fails here instead.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .plotlib import QUERY_CLASSES
from .runmeta import read_jsonl

PRODUCER = "c6_merge"
RECORD = "latency_op"
# What every class of one repetition must agree on: one engine, one corpus, one
# offered rate, one window, measured a class at a time. `query_class` is the one
# header field that must differ, which is why it is not in this list.
SHARED_FIELDS = ("engine", "engine_version", "corpus", "queries", "cache_state",
                 "offered_qps", "duration_s", "warmup_s", "concurrency",
                 "limit")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data")
    parser.add_argument("--config", required=True)
    parser.add_argument("--rep", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def class_log(source: Path, config: str, rep: str, query_class: str) -> Path:
    return source / f"c5-{config}-{query_class}-{rep}.jsonl"


def read_class(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        raise SystemExit(f"{path} is missing: C6 needs every one of "
                         f"{', '.join(QUERY_CLASSES)}, and a class drawn as an "
                         "empty gap reads as a finding about the engine")
    header, records = read_jsonl(str(path))
    if not records:
        raise SystemExit(f"{path} holds no records")
    return header, records


def assert_same_measurement(headers: dict[str, dict[str, Any]]) -> None:
    for field in SHARED_FIELDS:
        values = {header.get(field) for header in headers.values()}
        if len(values) > 1:
            detail = ", ".join(f"{name}={header.get(field)!r}"
                               for name, header in sorted(headers.items()))
            raise SystemExit(f"the classes disagree on {field} ({detail}); they "
                             "are not one repetition of one measurement")


def merged_header(headers: dict[str, dict[str, Any]], config: str,
                  rep: str) -> dict[str, Any]:
    first = headers[QUERY_CLASSES[0]]
    return {**first, "producer": PRODUCER, "config": config, "rep": rep,
            "query_class": "all",
            "merged_from": [f"c5-{config}-{name}-{rep}.jsonl"
                            for name in QUERY_CLASSES],
            "query_classes": list(QUERY_CLASSES)}


def merge(source: Path, config: str, rep: str) -> list[dict[str, Any]]:
    headers, records = {}, []
    for query_class in QUERY_CLASSES:
        header, class_records = read_class(class_log(source, config, rep,
                                                    query_class))
        headers[query_class] = header
        records.extend(class_records)
    assert_same_measurement(headers)
    return [merged_header(headers, config, rep), *records]


def write(records: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")


def main() -> int:
    args = parse_args()
    records = merge(Path(args.source), args.config, args.rep)
    write(records, Path(args.output))
    operations = sum(1 for record in records if record.get("record") == RECORD)
    print(f"{args.output}: {operations} operations over "
          f"{len(QUERY_CLASSES)} classes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
