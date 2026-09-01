"""Convert Wikimedia cirrus_search_index content shards into the canonical
corpus JSONL.

The cirrus_search_index dumps (successor to the deprecated cirrussearch
dumps) are already Elasticsearch bulk format — alternating action and source
lines, wikitext stripped to plain `text` — so no wiki markup parsing is
needed. A wiki's content is split into one or more numbered `.json.bz2`
shards (e.g. `enwiki_content-20260816-00000.json.bz2` ...
`-00064.json.bz2`); `--input` accepts a glob matching all of them and
processes one shard per worker (`--jobs`), merging the results in
shard-number order so the corpus is byte-identical to a serial pass. Each
output line: {"id", "uuid", "title", "text"}; the uuid is a deterministic
uuid5 of the page id, assigned by ftsbench.prepare_corpus.

Usage: python3 -m ftsbench.prepare_corpus --input 'data/20260816/index_name=simplewiki_content/simplewiki_content-20260816-*.json.bz2' --output data/corpus.jsonl
"""
import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, TextIO

from .corpus import open_text_auto

PROGRESS_EVERY_DOCS = 50_000
DEFAULT_MIN_CHARS = 200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True,
                        help="cirrus_search_index content shard(s): a single "
                             "file path or a glob matching multiple "
                             "*.json.bz2 shards")
    parser.add_argument("--output", required=True, help="canonical corpus JSONL path")
    parser.add_argument("--max-docs", type=int, default=0,
                        help="0 = no cap; a cap forces the serial path so "
                             "only the shards needed to reach it are read")
    parser.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS,
                        help="skip documents with shorter text")
    parser.add_argument("--jobs", type=int, default=os.cpu_count(),
                        help="shards converted concurrently, one per process")
    return parser.parse_args()


def resolve_shards(input_pattern: str) -> list[str]:
    shards = sorted(glob.glob(input_pattern))
    if not shards:
        raise FileNotFoundError(f"no files match --input {input_pattern!r}")
    return shards


def read_cirrus_pairs(paths: list[str]) -> Iterator[tuple[dict, dict]]:
    for path in paths:
        with open_text_auto(path) as dump:
            while True:
                action_line = dump.readline()
                source_line = dump.readline()
                if not source_line:
                    break
                yield json.loads(action_line), json.loads(source_line)


def extract_page_id(action: dict[str, Any]) -> int | None:
    raw_id = action.get("index", {}).get("_id")
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def to_corpus_doc(page_id: int, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": page_id,
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"wikipedia-page:{page_id}")),
        "title": source.get("title", ""),
        "text": source.get("text", ""),
    }


def convert_pairs(pairs: Iterator[tuple[dict, dict]], out: TextIO,
                  min_chars: int, max_docs: int = 0,
                  progress: bool = False) -> tuple[int, int]:
    written = skipped = 0
    for action, source in pairs:
        page_id = extract_page_id(action)
        if page_id is None:
            skipped += 1
            continue
        doc = to_corpus_doc(page_id, source)
        if len(doc["text"]) < min_chars:
            skipped += 1
            continue
        out.write(json.dumps(doc, ensure_ascii=False) + "\n")
        written += 1
        if progress and written % PROGRESS_EVERY_DOCS == 0:
            print(f"{written} docs written...", file=sys.stderr)
        if max_docs and written >= max_docs:
            break
    return written, skipped


def convert_serial(shards: list[str], output: str, min_chars: int,
                   max_docs: int) -> tuple[int, int]:
    with open(output, "w", encoding="utf-8") as out:
        return convert_pairs(read_cirrus_pairs(shards), out, min_chars,
                             max_docs, progress=True)


def convert_shard_to_part(shard: str, part: str, min_chars: int) -> tuple[int, int]:
    with open(part, "w", encoding="utf-8") as out:
        return convert_pairs(read_cirrus_pairs([shard]), out, min_chars)


def merge_parts(parts: list[str], output: str) -> None:
    with open(output, "w", encoding="utf-8") as out:
        for part in parts:
            with open(part, "r", encoding="utf-8") as part_file:
                shutil.copyfileobj(part_file, out)


def convert_parallel(shards: list[str], output: str, min_chars: int,
                     jobs: int) -> tuple[int, int]:
    workers = min(jobs, len(shards))
    output_dir = os.path.dirname(os.path.abspath(output))
    with tempfile.TemporaryDirectory(dir=output_dir, prefix=".parts-") as part_dir:
        parts = [os.path.join(part_dir, f"{i:05d}.part") for i in range(len(shards))]
        counts = run_shard_workers(shards, parts, min_chars, workers)
        merge_parts(parts, output)
    written = sum(w for w, _ in counts)
    skipped = sum(s for _, s in counts)
    return written, skipped


def run_shard_workers(shards: list[str], parts: list[str], min_chars: int,
                      workers: int) -> list[tuple[int, int]]:
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(convert_shard_to_part, shard, part, min_chars): shard
            for shard, part in zip(shards, parts)
        }
        done = 0
        for future in as_completed(futures):
            future.result()
            done += 1
            print(f"[{done}/{len(shards)}] {os.path.basename(futures[future])}",
                  file=sys.stderr)
    return [future.result()
            for future in sorted(futures, key=lambda f: futures[f])]


def run(input_pattern: str, output: str, max_docs: int, min_chars: int,
        jobs: int) -> tuple[int, int]:
    shards = resolve_shards(input_pattern)
    print(f"reading {len(shards)} shard(s): {shards[0]}"
          + (f" ... {shards[-1]}" if len(shards) > 1 else ""), file=sys.stderr)
    if max_docs or jobs <= 1 or len(shards) == 1:
        written, skipped = convert_serial(shards, output, min_chars, max_docs)
    else:
        written, skipped = convert_parallel(shards, output, min_chars, jobs)
    print(f"wrote {written} docs ({skipped} skipped) to {output}", file=sys.stderr)
    return written, skipped


def main() -> int:
    args = parse_args()
    run(input_pattern=args.input, output=args.output, max_docs=args.max_docs,
        min_chars=args.min_chars, jobs=args.jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
