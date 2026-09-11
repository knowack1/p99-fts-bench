"""A vector-store-shaped HTTP endpoint that reports the index the CQL sink holds.

Answers what `ftsbench.samplers.ScyllaSampler` and the Rust `scyllarate` harness
ask of a real vector-store: `/api/v1/indexes/{ks}/{idx}/status` for the document
count and the SERVING gate, and `/api/v1/info` for the version a run header
records. Nothing is indexed — the count is what the CQL half accepted, which is
the whole point: it makes the harness's build-rate number measurable against a
sink that cannot be the constraint.

**An index nobody created is 404, and so is one under another name.** The real
vector-store has no entry to answer for either, and answering a count anyway
would let a harness pointed at the wrong keyspace or index sail through its
own SERVING gate and report a complete, plausible, wrong build rate. A
misdirected status request is therefore refused *and* recorded, like every other
unexpected route.
"""
from __future__ import annotations

import asyncio
import json

from . import sink_http_wire
from .sink_counters import AcceptedWork
from .sink_http_wire import Request
from .sink_index import ModelledIndex

VERSION = "1.10.0-null-sink"
ENGINE = "null-sink — accept and discard, nothing is indexed"
STATUS_SUFFIX = "/status"
INDEXES_PREFIX = "/api/v1/indexes/"
INFO_PATH = "/api/v1/info"


def status_target(path: str) -> tuple[str, str] | None:
    """The `{keyspace}/{index}` a status request names, or None if not one."""
    if not path.startswith(INDEXES_PREFIX) or not path.endswith(STATUS_SUFFIX):
        return None
    middle = path[len(INDEXES_PREFIX):-len(STATUS_SUFFIX)]
    parts = middle.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


class Routes:
    """Path and method to a JSON reply, for one named index."""

    def __init__(self, work: AcceptedWork, index: ModelledIndex,
                 keyspace: str, name: str) -> None:
        self._work = work
        self._index = index
        self._target = (keyspace, name)

    def respond(self, request: Request) -> tuple[int, bytes]:
        path = request.path.split("?")[0]
        if request.method == "GET" and path == INFO_PATH:
            return 200, _json({"version": VERSION, "engine": ENGINE})
        target = status_target(path)
        if request.method == "GET" and target is not None:
            return self._status(target)
        return self._unanswered(request.method, path)

    def _status(self, target: tuple[str, str]) -> tuple[int, bytes]:
        if target != self._target:
            return self._unanswered(
                "GET", f"{INDEXES_PREFIX}{target[0]}/{target[1]}{STATUS_SUFFIX}")
        status = self._index.status()
        if status is None:
            return 404, _json({"error": "no such index",
                               "keyspace": self._target[0],
                               "index": self._target[1]})
        return 200, _json(status.as_json())

    def _unanswered(self, method: str, path: str) -> tuple[int, bytes]:
        self._work.note_unexpected(f"{method} {path}")
        return 404, _json({"error": "null sink does not answer this route",
                           "method": method, "path": path})


def _json(body: dict) -> bytes:
    return json.dumps(body).encode("utf-8")


async def serve(host: str, port: int, work: AcceptedWork, index: ModelledIndex,
                keyspace: str, name: str,
                delay_s: float = 0.0) -> asyncio.Server:
    return await sink_http_wire.serve(
        host, port, Routes(work, index, keyspace, name), delay_s)
