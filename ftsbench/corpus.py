"""Canonical corpus JSONL: one document per line with id / uuid / title / text."""
import bz2
import gzip
import json
from collections.abc import Iterable, Iterator
from typing import Any, TextIO


def open_text_auto(path: str) -> TextIO:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    if path.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def read_corpus(path: str, max_docs: int = 0) -> Iterator[dict[str, Any]]:
    with open_text_auto(path) as corpus_file:
        for line_number, line in enumerate(corpus_file):
            if max_docs and line_number >= max_docs:
                return
            yield json.loads(line)


def batched(items: Iterable, size: int) -> Iterator[list]:
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
