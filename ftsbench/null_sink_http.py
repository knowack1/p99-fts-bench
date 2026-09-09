"""An OpenSearch-shaped HTTP endpoint that accepts every `_bulk` and stores none.

Answers exactly what `ftsbench.opensearch_load` and `ftsbench.samplers` ask for:
the root version probe, index create, `_settings`, `_bulk`, `_refresh`, `_count`,
`_stats`, and the two node thread-pool endpoints the write-pool reading uses.
Everything else is answered 404 *and counted*, because a setup call that stopped
arriving would otherwise change the measurement in silence.

**A 2xx is not enough.** `opensearch_load.send_bulk` reads per-item statuses out
of a 200 response and raises if any failed, so the bulk reply has to carry one
item per action or a run against this sink would be measuring the error path.
The reply bodies are built once per item count and reused: at 512 documents per
bulk the loader sends thousands of identical-shaped responses, and serialising
each one would spend the sink's CPU on the very axis being measured.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass

from . import sink_tcp
from .sink_counters import AcceptedWork

HEADER_TERMINATOR = b"\r\n\r\n"
MAX_HEADER_BYTES = 1 << 16
DELETE_ACTION_PREFIX = b'{"delete"'
ACTION_PREFIX_BYTES = 16
VERSION = "2.19.0-null-sink"
SHARDS_OK = {"total": 1, "successful": 1, "failed": 0}
WRITE_POOL_SIZE = 3

STATUS_TEXT = {200: "OK", 404: "Not Found"}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: bytes


def line_offsets(payload: bytes) -> Iterator[tuple[int, int]]:
    position = 0
    while position < len(payload):
        end = payload.find(b"\n", position)
        if end < 0:
            end = len(payload)
        yield position, end
        position = end + 1


def bulk_action_count(payload: bytes) -> int:
    """Actions in one `_bulk` body, by the NDJSON grammar.

    Counted by walking the alternation rather than halving the line count,
    because a `delete` action carries no source line: a churn bulk mixing adds
    and deletes would otherwise be reported as fewer documents than it offered.
    """
    lines = [(start, end) for start, end in line_offsets(payload)
             if end > start]
    count, index = 0, 0
    while index < len(lines):
        start, _ = lines[index]
        count += 1
        is_delete = payload[start:start + ACTION_PREFIX_BYTES].startswith(
            DELETE_ACTION_PREFIX)
        index += 1 if is_delete else 2
    return count


class BulkReplies:
    """Bulk response bodies, one per item count, built on first use."""

    def __init__(self) -> None:
        self._bodies: dict[int, bytes] = {}

    def body(self, items: int) -> bytes:
        cached = self._bodies.get(items)
        if cached is None:
            cached = json.dumps({
                "took": 0,
                "errors": False,
                "items": [{"index": {"status": 201, "result": "created"}}]
                * items,
            }).encode("utf-8")
            self._bodies[items] = cached
        return cached


def index_stats(docs: int) -> dict:
    """The `_all.total` subtree `samplers.OpenSearchSampler.sample()` reads.

    Every counter that is genuinely unknowable here is 0 rather than absent: the
    sampler indexes into these keys, and a missing one would fail the monitor
    rather than record a sink that does not merge or refresh.
    """
    return {
        "_all": {"total": {
            "docs": {"count": docs, "deleted": 0},
            "indexing": {"index_total": docs, "index_current": 0},
            "segments": {"count": 0, "memory_in_bytes": 0},
            "merges": {"current": 0, "current_docs": 0, "total": 0,
                       "total_docs": 0, "total_time_in_millis": 0},
            "refresh": {"total": 0, "total_time_in_millis": 0},
            "store": {"size_in_bytes": 0},
        }},
    }


def node_thread_pool_stats() -> dict:
    return {"nodes": {"null-sink": {"thread_pool": {
        "write": {"active": 0, "queue": 0, "rejected": 0,
                  "threads": WRITE_POOL_SIZE},
    }}}}


def node_thread_pool_info() -> dict:
    return {"nodes": {"null-sink": {"thread_pool": {
        "write": {"type": "fixed", "size": WRITE_POOL_SIZE},
    }}}}


def is_bulk(path: str) -> bool:
    return path.rstrip("/").endswith("_bulk")


def is_suffix(path: str, suffix: str) -> bool:
    return path.split("?")[0].rstrip("/").endswith(suffix)


class Routes:
    """Path and method to a JSON reply, with `_bulk` counted on the way past."""

    def __init__(self, work: AcceptedWork) -> None:
        self._work = work
        self._replies = BulkReplies()

    def respond(self, request: Request) -> tuple[int, bytes]:
        if request.method == "POST" and is_bulk(request.path):
            return self._bulk(request.body)
        return self._control(request)

    def _bulk(self, payload: bytes) -> tuple[int, bytes]:
        items = bulk_action_count(payload)
        self._work.add(ops=1, docs=items)
        return 200, self._replies.body(items)

    def _control(self, request: Request) -> tuple[int, bytes]:
        path = request.path.split("?")[0]
        for route in (self._progress_route, self._admin_route):
            answer = route(request.method, path)
            if answer is not None:
                return 200, answer
        self._work.note_unexpected(f"{request.method} {path}")
        return 404, _json({"error": "null sink does not answer this route",
                           "method": request.method, "path": path})

    def _progress_route(self, method: str, path: str) -> bytes | None:
        """What a sampler reads: version, counts, index stats, write pool."""
        if method != "GET":
            return None
        if path == "/":
            return _json({"name": "null-sink", "version": {"number": VERSION}})
        if is_suffix(path, "_count"):
            return _json({"count": self._work.docs, "_shards": SHARDS_OK})
        if is_suffix(path, "_stats"):
            return _json(index_stats(self._work.docs))
        if path == "/_nodes/stats/thread_pool":
            return _json(node_thread_pool_stats())
        if path == "/_nodes/thread_pool":
            return _json(node_thread_pool_info())
        return None

    def _admin_route(self, method: str, path: str) -> bytes | None:
        """What a loader does around a load: create, tune, refresh, probe."""
        if method == "POST" and is_suffix(path, "_refresh"):
            return _json({"_shards": SHARDS_OK})
        if method == "PUT" and is_suffix(path, "_settings"):
            return _json({"acknowledged": True})
        if method in ("PUT", "DELETE") and path.count("/") == 1:
            return _json({"acknowledged": True, "index": path[1:]})
        if method == "HEAD":
            return b""
        return None


def _json(body: dict) -> bytes:
    return json.dumps(body).encode("utf-8")


def http_response(status: int, body: bytes) -> bytes:
    reason = STATUS_TEXT.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: keep-alive\r\n\r\n").encode("ascii")
    return head + body


def content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            return int(value.strip())
    return 0


def request_line(head: bytes) -> tuple[str, str]:
    parts = head.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2:
        raise ValueError(f"malformed request line: {parts!r}")
    return parts[0].decode("ascii"), parts[1].decode("latin-1")


async def read_head(reader: asyncio.StreamReader) -> bytes | None:
    try:
        return await reader.readuntil(HEADER_TERMINATOR)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    except asyncio.LimitOverrunError as overrun:
        raise ValueError(f"request head over {MAX_HEADER_BYTES} bytes "
                         f"({overrun.consumed} consumed)") from overrun


async def read_request(reader: asyncio.StreamReader) -> Request | None:
    head = await read_head(reader)
    if not head:
        return None
    method, path = request_line(head)
    body = await reader.readexactly(content_length(head))
    return Request(method, path, body)


async def serve_connection(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, routes: Routes,
                           delay_s: float) -> None:
    handle = sink_tcp.accepted_socket(writer)
    try:
        while True:
            request = await read_request(reader)
            if request is None:
                return
            sink_tcp.acknowledge_now(handle)
            status, body = routes.respond(request)
            if delay_s:
                await asyncio.sleep(delay_s)
            writer.write(http_response(status, body))
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        return
    finally:
        writer.close()


async def serve(host: str, port: int, work: AcceptedWork,
                delay_s: float = 0.0) -> asyncio.Server:
    routes = Routes(work)

    async def client_connected(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        await serve_connection(reader, writer, routes, delay_s)

    return await asyncio.start_server(client_connected, host, port,
                                      limit=MAX_HEADER_BYTES)
