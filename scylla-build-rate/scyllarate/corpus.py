"""The canonical corpus as ScyllaDB bound parameters.

`article_id` is the corpus's own deterministic uuid5 of the page id, so every
sweep point overwrites the same rows instead of growing the table.
"""
import json
import uuid
from collections.abc import Iterator
from typing import Any

InsertParams = tuple[uuid.UUID, int, str, str]


def _read_documents(path: str, max_docs: int = 0) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for seen, line in enumerate(handle):
            if max_docs and seen >= max_docs:
                return
            yield json.loads(line)


def _to_insert_params(doc: dict[str, Any]) -> InsertParams:
    return (uuid.UUID(doc["uuid"]), doc["id"], doc["title"], doc["text"])


def read_insert_params(path: str, max_docs: int = 0) -> Iterator[InsertParams]:
    return (_to_insert_params(doc) for doc in _read_documents(path, max_docs))
