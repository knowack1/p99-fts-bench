"""HTTP/1.1 framing for the null sinks, with no opinion about what it answers.

Two sinks now speak HTTP and they are shaped by two different engines: the
OpenSearch-shaped one in `null_sink_http` and the vector-store-shaped one in
`null_sink_vstore`. The wire between them is the same — answer every whole
request the last read completed, write the replies once, keep the connection
alive — so it lives here rather than once per sink, and a routing table is
whatever object answers `respond(request) -> (status, body)`.

Keep-alive and **one write per read** are not incidental. The loaders hold many
requests outstanding on few connections, so a read commonly carries several
whole requests, and answering them one at a time puts a `recvfrom`, a `sendto`
and a `setsockopt` between the client and its own ceiling — per document, once
`osrate --batch-size 1` makes a request a document. `sink_tcp` already states
the rule this restores: one `setsockopt` per read, not per document.

That cost is measured, not assumed. Reading one request per iteration cost 6.04
syscalls per document against 0.36 for `null_sink_cql`, which has always
drained its whole buffer per read; the HTTP sink pegged its one Python thread
at ~45,000 docs/s while the CQL sink reached ~131,000 on the same box, the same
corpus and the same ladder. Draining per read removes the per-request buffer
churn outright — 603,782 syscalls per 100,000 documents became 306,759, and the
sink reached ~61,000 docs/s. So `answers_for` mirrors
`null_sink_cql.answers_for` deliberately, down to the name.

What remains is HTTP/1.1 itself rather than this loop, and no parser will move
it. Keep-alive is serial reuse: each of `--concurrency` sockets carries one
outstanding request at a time, so a read cannot hold more than one and there is
nothing left to batch. CQL multiplexes many statements over one connection by
stream id, which is why its reads take ~14 frames at once. Past the point where
every connection is busy, closing the gap needs more than one core — not a
cheaper parse.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from . import sink_tcp

HEADER_TERMINATOR = b"\r\n\r\n"
MAX_HEADER_BYTES = 1 << 16
READ_CHUNK_BYTES = 1 << 16

STATUS_TEXT = {200: "OK", 404: "Not Found"}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: bytes


class Routes(Protocol):
    def respond(self, request: Request) -> tuple[int, bytes]:
        ...


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


def refuse_an_unbounded_head(size: int) -> None:
    if size > MAX_HEADER_BYTES:
        raise ValueError(f"request head over {MAX_HEADER_BYTES} bytes "
                         f"({size} buffered)")


def take_request(buffer: bytes, offset: int) -> tuple[Request | None, int]:
    """The request starting at `offset`, and the offset after it.

    Returns `(None, offset)` when the buffer does not yet hold a whole request,
    so a caller can keep the partial bytes and read more. The caller advances
    once per read rather than trimming per request, which is why the cursor is
    returned instead of the buffer being consumed here.
    """
    head_end = buffer.find(HEADER_TERMINATOR, offset)
    if head_end < 0:
        refuse_an_unbounded_head(len(buffer) - offset)
        return None, offset
    refuse_an_unbounded_head(head_end - offset)
    head = bytes(buffer[offset:head_end])
    method, path = request_line(head)
    body_start = head_end + len(HEADER_TERMINATOR)
    end = body_start + content_length(head)
    if len(buffer) < end:
        return None, offset
    return Request(method, path, bytes(buffer[body_start:end])), end


def answers_for(buffer: bytearray, routes: Routes) -> bytes:
    """Every whole request in the buffer, answered, with the rest left behind.

    Replies are concatenated and written once per read rather than per request,
    for the reason in the module docstring. They are returned in request order,
    which HTTP pipelining requires: a client pairing replies to requests by
    position would otherwise mis-pair them without either side erroring.
    """
    cursor, out = 0, []
    while True:
        request, cursor = take_request(buffer, cursor)
        if request is None:
            break
        out.append(http_response(*routes.respond(request)))
    del buffer[:cursor]
    return b"".join(out)


async def serve_connection(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, routes: Routes,
                           delay_s: float) -> None:
    handle = sink_tcp.accepted_socket(writer)
    buffer = bytearray()
    try:
        while True:
            chunk = await reader.read(READ_CHUNK_BYTES)
            if not chunk:
                return
            sink_tcp.acknowledge_now(handle)
            buffer += chunk
            payload = answers_for(buffer, routes)
            if not payload:
                continue
            if delay_s:
                await asyncio.sleep(delay_s)
            writer.write(payload)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        return
    finally:
        writer.close()


async def serve(host: str, port: int, routes: Routes,
                delay_s: float = 0.0) -> asyncio.Server:
    async def client_connected(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        await serve_connection(reader, writer, routes, delay_s)

    return await asyncio.start_server(client_connected, host, port,
                                      limit=MAX_HEADER_BYTES)
